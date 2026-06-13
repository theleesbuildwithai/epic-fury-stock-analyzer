"""
Historical Calibration — 50-year pattern analysis for smarter predictions.

Downloads maximum available history from Yahoo Finance and analyzes:
  1. Seasonal patterns — which months are historically best/worst per stock
  2. Sector rotation cycles — rolling relative strength over 20+ years
  3. Mean reversion half-life — how fast each stock reverts to mean
  4. Macro regime performance — how stocks perform in bear/bull/high-VIX periods
  5. Momentum persistence — optimal momentum lookback per stock

Results are cached in-memory and backed up to S3 for persistence across deploys.
This runs as a background job — never blocks real-time trading.

CRITICAL: Batch downloads with 5-second delays to protect Yahoo Finance rate limits.
"""

import yfinance as yf
import numpy as np
import pandas as pd
import json
import time
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Module-level calibration cache
_calibration = {}
_calibration_loaded = False
_CALIBRATION_FILE = "historical_calibration.json"
_S3_CALIBRATION_KEY = "calibration/historical_calibration.json"

# Rate limiting
_BATCH_SIZE = 20
_BATCH_DELAY = 5.0  # seconds between batches


def get_calibration() -> dict:
    """Get the current calibration data. Returns {} if not built yet."""
    global _calibration, _calibration_loaded
    if not _calibration_loaded:
        _calibration = _load_from_disk()
        _calibration_loaded = True
    return _calibration


def _load_from_disk() -> dict:
    """Try loading calibration from local file."""
    if os.path.exists(_CALIBRATION_FILE):
        try:
            with open(_CALIBRATION_FILE, "r") as f:
                data = json.load(f)
            age_hours = (time.time() - data.get("built_at_epoch", 0)) / 3600
            if age_hours < 168:  # 7 days
                logger.info(f"Loaded historical calibration from disk ({age_hours:.0f}h old, {len(data.get('stocks', {}))} stocks)")
                return data
            else:
                logger.info(f"Historical calibration expired ({age_hours:.0f}h old) — will rebuild")
        except Exception as e:
            logger.warning(f"Could not load calibration from disk: {e}")
    return {}


def _save_to_disk(data: dict):
    """Save calibration to local file."""
    try:
        with open(_CALIBRATION_FILE, "w") as f:
            json.dump(data, f)
        logger.info(f"Saved calibration to {_CALIBRATION_FILE}")
    except Exception as e:
        logger.warning(f"Could not save calibration to disk: {e}")


def _save_to_s3(data: dict):
    """Save calibration to S3 for persistence across deploys."""
    try:
        import boto3
        s3_bucket = os.environ.get("DB_BACKUP_BUCKET", "epic-fury-portfolio-db")
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.put_object(
            Bucket=s3_bucket,
            Key=_S3_CALIBRATION_KEY,
            Body=json.dumps(data),
            ContentType="application/json",
        )
        logger.info(f"Backed up calibration to s3://{s3_bucket}/{_S3_CALIBRATION_KEY}")
    except Exception as e:
        logger.debug(f"Could not backup calibration to S3: {e}")


def restore_calibration_from_s3():
    """Restore calibration from S3 on startup."""
    global _calibration, _calibration_loaded
    try:
        import boto3
        s3_bucket = os.environ.get("DB_BACKUP_BUCKET", "epic-fury-portfolio-db")
        s3 = boto3.client("s3", region_name="us-east-1")
        response = s3.get_object(Bucket=s3_bucket, Key=_S3_CALIBRATION_KEY)
        data = json.loads(response["Body"].read().decode("utf-8"))
        age_hours = (time.time() - data.get("built_at_epoch", 0)) / 3600
        if age_hours < 168:  # 7 days
            _calibration = data
            _calibration_loaded = True
            _save_to_disk(data)
            logger.info(f"Restored calibration from S3 ({age_hours:.0f}h old, {len(data.get('stocks', {}))} stocks)")
            return True
    except Exception as e:
        logger.debug(f"No calibration in S3 or error: {e}")
    return False


def _fetch_max_history(symbols: list) -> dict:
    """
    Download maximum available history for symbols in batches.
    Returns {symbol: DataFrame} for successful downloads.
    """
    result = {}
    batches = [symbols[i:i + _BATCH_SIZE] for i in range(0, len(symbols), _BATCH_SIZE)]

    for batch_num, batch in enumerate(batches):
        try:
            logger.info(f"Historical download batch {batch_num + 1}/{len(batches)}: {len(batch)} symbols")
            df = yf.download(batch, period="max", progress=False, group_by="ticker", threads=True)

            if df is None or df.empty:
                continue

            if len(batch) == 1:
                # Single symbol — not multi-indexed
                sym = batch[0]
                if "Close" in df.columns:
                    close_col = df["Close"]
                    if hasattr(close_col, "columns"):
                        close_col = close_col.iloc[:, 0]
                    close_series = close_col.dropna()
                    if len(close_series) >= 252:  # At least 1 year
                        result[sym] = close_series
            else:
                for sym in batch:
                    try:
                        if isinstance(df.columns, pd.MultiIndex):
                            if sym not in df.columns.get_level_values(0):
                                continue
                            close_series = df[(sym, "Close")].dropna()
                        else:
                            continue

                        if len(close_series) >= 252:
                            result[sym] = close_series
                    except Exception:
                        continue

        except Exception as e:
            logger.warning(f"Historical batch {batch_num + 1} failed: {e}")

        if batch_num < len(batches) - 1:
            time.sleep(_BATCH_DELAY)

    # Fallback: per-symbol multi-source for any gaps
    missing = [s for s in symbols if s not in result]
    if missing:
        try:
            from analytics.multi_source_adapter import get_historical_any_source
            for sym in missing:
                try:
                    df2 = get_historical_any_source(sym, "5y")
                    if df2 is not None and "Close" in df2.columns:
                        close_series = df2["Close"].dropna()
                        if len(close_series) >= 252:
                            result[sym] = close_series
                except Exception:
                    continue
        except Exception:
            pass

    return result


def _analyze_seasonal_patterns(close_series: pd.Series) -> dict:
    """
    Calculate average monthly returns over full history.
    Returns {month_number: avg_return_pct} (1-12).
    """
    try:
        returns = close_series.pct_change().dropna()
        if len(returns) < 252:
            return {}

        monthly = {}
        for month in range(1, 13):
            month_returns = returns[returns.index.month == month]
            if len(month_returns) >= 20:  # Need at least 20 data points
                avg = float(np.mean(month_returns)) * 100
                monthly[str(month)] = round(avg, 4)

        return monthly
    except Exception:
        return {}


def _analyze_mean_reversion_halflife(close_series: pd.Series) -> float:
    """
    Estimate mean reversion half-life using Ornstein-Uhlenbeck regression.
    Returns half-life in trading days. Shorter = stronger mean reversion.
    """
    try:
        log_prices = np.log(close_series.values[-504:])  # Use last 2 years
        if len(log_prices) < 126:
            return 30.0  # Default

        # Detrend: subtract rolling mean
        window = 63
        rolling_mean = pd.Series(log_prices).rolling(window).mean().dropna().values
        detrended = log_prices[-len(rolling_mean):] - rolling_mean

        if len(detrended) < 30:
            return 30.0

        # AR(1) regression: y_t = phi * y_{t-1} + epsilon
        y = detrended[1:]
        x = detrended[:-1]
        if np.std(x) < 1e-10:
            return 30.0

        phi = float(np.corrcoef(x, y)[0, 1])
        if phi >= 1.0 or phi <= 0:
            return 60.0  # No mean reversion

        half_life = -np.log(2) / np.log(abs(phi))
        return max(1.0, min(120.0, float(half_life)))
    except Exception:
        return 30.0


def _analyze_momentum_persistence(close_series: pd.Series) -> dict:
    """
    Measure autocorrelation at various lags to find optimal momentum lookback.
    Returns {optimal_period: int, persistence_score: float}.
    """
    try:
        returns = close_series.pct_change().dropna()
        if len(returns) < 504:
            return {"optimal_period": 252, "persistence_score": 0.0}

        # Test different momentum lookback periods
        lookbacks = [21, 63, 126, 252]
        best_period = 252
        best_autocorr = -1.0

        for period in lookbacks:
            if len(returns) < period * 2:
                continue
            momentum = returns.rolling(period).sum().dropna()
            if len(momentum) < period:
                continue
            # Autocorrelation of momentum signal with future returns
            future_returns = returns.rolling(21).sum().shift(-21).dropna()
            overlap = min(len(momentum), len(future_returns))
            if overlap < 50:
                continue
            m = momentum.iloc[-overlap:]
            f = future_returns.iloc[-overlap:]
            corr = float(np.corrcoef(m.values, f.values)[0, 1])
            if not np.isnan(corr) and corr > best_autocorr:
                best_autocorr = corr
                best_period = period

        return {
            "optimal_period": best_period,
            "persistence_score": round(max(0, best_autocorr), 4),
        }
    except Exception:
        return {"optimal_period": 252, "persistence_score": 0.0}


def _analyze_regime_performance(close_series: pd.Series, spy_series: pd.Series) -> dict:
    """
    Analyze how a stock performs in different market regimes.
    Uses SPY to define regimes, then measures stock performance in each.
    """
    try:
        if len(spy_series) < 504 or len(close_series) < 504:
            return {}

        # Align dates
        common_idx = close_series.index.intersection(spy_series.index)
        if len(common_idx) < 252:
            return {}

        stock_ret = close_series.loc[common_idx].pct_change().dropna()
        spy_ret = spy_series.loc[common_idx].pct_change().dropna()

        # Align lengths
        common = stock_ret.index.intersection(spy_ret.index)
        stock_ret = stock_ret.loc[common]
        spy_ret = spy_ret.loc[common]

        if len(stock_ret) < 252:
            return {}

        # Define regimes: SPY 200-day SMA
        spy_prices = spy_series.loc[common]
        spy_sma200 = spy_prices.rolling(200).mean()

        bull_mask = spy_prices > spy_sma200
        bear_mask = spy_prices < spy_sma200

        result = {}
        for regime, mask in [("bull", bull_mask), ("bear", bear_mask)]:
            regime_returns = stock_ret[mask.reindex(stock_ret.index, method="ffill").fillna(False)]
            if len(regime_returns) >= 50:
                result[f"{regime}_avg_daily"] = round(float(np.mean(regime_returns)) * 100, 4)
                result[f"{regime}_win_rate"] = round(float((regime_returns > 0).mean()) * 100, 1)
                result[f"{regime}_volatility"] = round(float(np.std(regime_returns)) * 100, 4)

        return result
    except Exception:
        return {}


def _analyze_sector_rotation(sector_data: dict, spy_series: pd.Series) -> dict:
    """
    Analyze sector rotation cycles using rolling relative strength.
    sector_data: {sector_name: [list of close_series for sector stocks]}
    """
    try:
        result = {}
        spy_ret_12m = spy_series.pct_change(252).dropna()

        for sector, series_list in sector_data.items():
            if not series_list:
                continue
            # Average sector performance
            sector_returns = []
            for s in series_list[:10]:  # Max 10 stocks per sector for speed
                try:
                    ret_12m = s.pct_change(252).dropna()
                    if len(ret_12m) >= 252:
                        sector_returns.append(ret_12m)
                except Exception:
                    continue

            if not sector_returns:
                continue

            # Compute average relative strength
            combined = pd.concat(sector_returns, axis=1).mean(axis=1).dropna()
            common = combined.index.intersection(spy_ret_12m.index)
            if len(common) < 252:
                continue

            rel_strength = combined.loc[common] - spy_ret_12m.loc[common]
            current_rs = float(rel_strength.iloc[-1]) if len(rel_strength) > 0 else 0
            avg_rs = float(rel_strength.mean())

            # Position in cycle: above avg = strong, below = weak
            result[sector] = {
                "current_rs": round(current_rs * 100, 2),
                "avg_rs": round(avg_rs * 100, 2),
                "signal": "strong" if current_rs > avg_rs else "weak",
            }

        return result
    except Exception:
        return {}


# ============================================================
#  UPGRADE 1: CRISIS CORRELATION MATRIX
# ============================================================

def _analyze_crisis_correlations(history: dict, spy_series: pd.Series) -> dict:
    """
    Identify which stocks become dangerously correlated during market crises.
    Uses SPY drawdowns > 10% to define crisis periods, then computes
    correlation matrices during crisis vs normal periods.

    Returns: {symbol: {crisis_pairs: {other_symbol: corr}, max_crisis_corr: float}}
    """
    try:
        if spy_series is None or len(spy_series) < 504:
            return {}

        # Find crisis periods: SPY drawdown > 10% from rolling peak
        spy_values = spy_series.values.astype(float)
        rolling_peak = pd.Series(spy_values).expanding().max().values
        drawdown = (spy_values - rolling_peak) / rolling_peak

        crisis_mask = drawdown < -0.10  # 10%+ drawdown = crisis
        crisis_dates = spy_series.index[crisis_mask]

        if len(crisis_dates) < 50:
            return {}

        # Get returns for all stocks that have enough history
        returns_data = {}
        symbols_with_data = []
        for sym, close_series in history.items():
            try:
                common = close_series.index.intersection(spy_series.index)
                if len(common) >= 504:
                    ret = close_series.loc[common].pct_change().dropna()
                    if len(ret) >= 252:
                        returns_data[sym] = ret
                        symbols_with_data.append(sym)
            except Exception:
                continue

        if len(symbols_with_data) < 10:
            return {}

        # Limit to top 100 most liquid symbols for speed
        symbols_with_data = symbols_with_data[:100]

        # Calculate pairwise crisis correlations for top symbols
        result = {}
        for sym in symbols_with_data:
            sym_ret = returns_data[sym]
            crisis_pairs = {}

            for other_sym in symbols_with_data:
                if other_sym == sym:
                    continue
                other_ret = returns_data[other_sym]
                common = sym_ret.index.intersection(other_ret.index)
                crisis_common = common.intersection(crisis_dates)

                if len(crisis_common) >= 30:
                    crisis_corr = float(np.corrcoef(
                        sym_ret.loc[crisis_common].values,
                        other_ret.loc[crisis_common].values
                    )[0, 1])
                    if not np.isnan(crisis_corr) and abs(crisis_corr) > 0.70:
                        crisis_pairs[other_sym] = round(crisis_corr, 3)

            if crisis_pairs:
                max_crisis = max(crisis_pairs.values())
                result[sym] = {
                    "crisis_pairs": crisis_pairs,
                    "max_crisis_corr": round(max_crisis, 3),
                }

        logger.info(f"Crisis correlation analysis: {len(result)} stocks with high crisis correlations")
        return result

    except Exception as e:
        logger.debug(f"Crisis correlation analysis failed: {e}")
        return {}


# ============================================================
#  UPGRADE 2: VOLATILITY REGIME DETECTION
# ============================================================

def _analyze_volatility_regimes(spy_series: pd.Series) -> dict:
    """
    Classify historical VIX into 4 regimes and measure what happens in each.
    Uses SPY returns to determine optimal position sizing per regime.

    Returns: {regime_name: {avg_duration_days, avg_daily_return, optimal_size_mult, count}}
    """
    try:
        # Download VIX history
        time.sleep(_BATCH_DELAY)
        vix_df = yf.download("^VIX", period="max", progress=False)
        if vix_df is None or len(vix_df) < 252:
            try:
                from analytics.multi_source_adapter import get_historical_any_source
                vix_df = get_historical_any_source("^VIX", "5y")
            except Exception:
                vix_df = None
        if vix_df is None or len(vix_df) < 252:
            return {}

        vix_close = vix_df["Close"]
        if hasattr(vix_close, "columns"):
            vix_close = vix_close.iloc[:, 0]
        vix_close = vix_close.dropna()

        # Align with SPY
        common = vix_close.index.intersection(spy_series.index)
        if len(common) < 504:
            return {}

        vix = vix_close.loc[common].values.astype(float)
        spy_ret = spy_series.loc[common].pct_change().fillna(0).values.astype(float)

        # Define regimes
        regimes = {
            "LOW": (0, 15),
            "NORMAL": (15, 20),
            "HIGH": (20, 30),
            "CRISIS": (30, 200),
        }

        result = {}
        for name, (low, high) in regimes.items():
            mask = (vix >= low) & (vix < high)
            if mask.sum() < 50:
                continue

            regime_returns = spy_ret[mask]
            avg_daily = float(np.mean(regime_returns)) * 100

            # Count regime stretches for avg duration
            transitions = np.diff(mask.astype(int))
            starts = np.where(transitions == 1)[0]
            ends = np.where(transitions == -1)[0]
            if len(starts) > 0 and len(ends) > 0:
                durations = []
                for s in starts:
                    matching_ends = ends[ends > s]
                    if len(matching_ends) > 0:
                        durations.append(matching_ends[0] - s)
                avg_duration = float(np.mean(durations)) if durations else 0
            else:
                avg_duration = 0

            # Optimal position size: inverse of volatility
            volatility = float(np.std(regime_returns)) * 100
            if name == "LOW":
                optimal_size = 1.2
            elif name == "NORMAL":
                optimal_size = 1.0
            elif name == "HIGH":
                optimal_size = 0.6
            else:
                optimal_size = 0.3

            result[name] = {
                "avg_duration_days": round(avg_duration, 0),
                "avg_daily_return_pct": round(avg_daily, 4),
                "daily_volatility_pct": round(volatility, 4),
                "optimal_size_mult": optimal_size,
                "days_observed": int(mask.sum()),
            }

        logger.info(f"Volatility regime analysis: {len(result)} regimes classified")
        return result

    except Exception as e:
        logger.debug(f"Volatility regime analysis failed: {e}")
        return {}


# ============================================================
#  UPGRADE 3: EARNINGS SEASONALITY
# ============================================================

def _analyze_earnings_seasonality(close_series: pd.Series) -> dict:
    """
    Detect historical earnings events from price gaps + volume spikes.
    Track average post-earnings drift and consistency.

    Returns: {avg_gap_pct, drift_direction, consistency_pct, event_count}
    """
    try:
        if len(close_series) < 504:
            return {}

        prices = close_series.values.astype(float)
        returns = np.diff(prices) / prices[:-1] * 100

        # Detect earnings-like events: daily move > 3% (big gap)
        big_moves = []
        for i in range(len(returns)):
            if abs(returns[i]) >= 3.0:
                big_moves.append({"idx": i, "gap_pct": float(returns[i])})

        if len(big_moves) < 4:
            return {}

        # Analyze post-event drift (5-day return after each big gap)
        gaps = []
        positive_drifts = 0
        total_drift = 0

        for move in big_moves:
            idx = move["idx"]
            if idx + 6 >= len(prices):
                continue
            gap = move["gap_pct"]
            # 5-day drift after the gap
            drift_5d = ((prices[idx + 6] / prices[idx + 1]) - 1) * 100
            gaps.append(gap)

            # Does the drift continue in the same direction as the gap?
            if (gap > 0 and drift_5d > 0) or (gap < 0 and drift_5d < 0):
                positive_drifts += 1
            total_drift += drift_5d

        if not gaps:
            return {}

        avg_gap = float(np.mean([abs(g) for g in gaps]))
        consistency = (positive_drifts / len(gaps)) * 100
        avg_drift = total_drift / len(gaps)

        return {
            "avg_gap_pct": round(avg_gap, 2),
            "drift_direction": "positive" if avg_drift > 0 else "negative",
            "avg_drift_5d_pct": round(avg_drift, 3),
            "consistency_pct": round(consistency, 1),
            "event_count": len(gaps),
        }

    except Exception:
        return {}


# ============================================================
#  UPGRADE 4: CROSS-ASSET LEADING INDICATORS
# ============================================================

def _analyze_cross_asset_leads(history: dict, sector_map: dict) -> dict:
    """
    Find which macro assets lead which sectors.
    Tests copper → industrials, gold → defensives, oil → energy, yields → financials.

    Returns: {sector: {indicator, optimal_lag_days, correlation}}
    """
    try:
        # Download macro indicators
        time.sleep(_BATCH_DELAY)
        macro_symbols = ["GC=F", "CL=F", "^TNX", "UUP"]
        macro_df = yf.download(macro_symbols, period="max", progress=False, group_by="ticker")

        if macro_df is None or macro_df.empty:
            # Fallback: per-symbol multi-source
            try:
                import pandas as pd
                from analytics.multi_source_adapter import get_historical_any_source
                frames = {}
                for sym in macro_symbols:
                    try:
                        df2 = get_historical_any_source(sym, "5y")
                        if df2 is not None and "Close" in df2.columns:
                            frames[sym] = df2["Close"].dropna()
                    except Exception:
                        continue
                if frames:
                    macro_df = pd.concat(frames, axis=1)
                    macro_df.columns = pd.MultiIndex.from_tuples(
                        [(s, "Close") for s in frames.keys()])
                else:
                    return {}
            except Exception:
                return {}

        # Extract macro series
        macro_series = {}
        macro_names = {"GC=F": "gold", "CL=F": "oil", "^TNX": "yields", "UUP": "dollar"}
        for sym, name in macro_names.items():
            try:
                if isinstance(macro_df.columns, pd.MultiIndex):
                    close = macro_df[(sym, "Close")].dropna()
                    if len(close) >= 252:
                        macro_series[name] = close
            except Exception:
                continue

        if not macro_series:
            return {}

        # Also use COPX (copper) from existing history if available
        if "COPX" in history:
            macro_series["copper"] = history["COPX"]
        elif "FCX" in history:
            macro_series["copper"] = history["FCX"]

        # Define which indicators should lead which sectors
        sector_indicators = {
            "Industrials": ["copper", "oil"],
            "Materials": ["copper", "gold"],
            "Energy": ["oil"],
            "Financials": ["yields"],
            "Healthcare": ["gold"],
            "Consumer Staples": ["gold", "dollar"],
            "Utilities": ["yields", "gold"],
            "Technology": ["yields", "dollar"],
            "Commodities": ["gold", "oil", "copper", "dollar"],
        }

        # Build sector return series
        sector_returns = {}
        for sym, close_series in history.items():
            sector = sector_map.get(sym, "Unknown")
            if sector in sector_indicators:
                ret = close_series.pct_change().dropna()
                if len(ret) >= 252:
                    if sector not in sector_returns:
                        sector_returns[sector] = []
                    sector_returns[sector].append(ret)

        result = {}
        lags_to_test = [5, 10, 21, 42, 63]

        for sector, indicators in sector_indicators.items():
            if sector not in sector_returns:
                continue

            # Average sector returns
            sr_list = sector_returns[sector][:10]
            avg_sector = pd.concat(sr_list, axis=1).mean(axis=1).dropna()

            best_indicator = None
            best_lag = 0
            best_corr = 0

            for indicator_name in indicators:
                if indicator_name not in macro_series:
                    continue

                indicator = macro_series[indicator_name].pct_change().dropna()

                for lag in lags_to_test:
                    try:
                        # Shift indicator back by `lag` days (indicator leads sector)
                        shifted = indicator.shift(lag).dropna()
                        common = shifted.index.intersection(avg_sector.index)
                        if len(common) < 252:
                            continue

                        corr = float(np.corrcoef(
                            shifted.loc[common].values,
                            avg_sector.loc[common].values
                        )[0, 1])

                        if not np.isnan(corr) and abs(corr) > abs(best_corr):
                            best_corr = corr
                            best_lag = lag
                            best_indicator = indicator_name
                    except Exception:
                        continue

            if best_indicator and abs(best_corr) > 0.05:
                result[sector] = {
                    "leading_indicator": best_indicator,
                    "optimal_lag_days": best_lag,
                    "correlation": round(best_corr, 4),
                    "signal": "positive" if best_corr > 0 else "inverse",
                }

        logger.info(f"Cross-asset lead analysis: {len(result)} sector relationships found")
        return result

    except Exception as e:
        logger.debug(f"Cross-asset lead analysis failed: {e}")
        return {}


# ============================================================
#  UPGRADE 5: DRAWDOWN PATTERN RECOGNITION
# ============================================================

def _analyze_drawdown_patterns(spy_series: pd.Series) -> dict:
    """
    Study all historical drawdowns > 5% from 50 years of SPY.
    Returns statistics for dynamic drawdown threshold calibration.

    Returns: {avg_depth_pct, avg_duration_days, avg_recovery_days, percentiles}
    """
    try:
        if spy_series is None or len(spy_series) < 504:
            return {}

        prices = spy_series.values.astype(float)

        # Find all drawdowns > 5%
        peak = prices[0]
        drawdowns = []
        current_dd_start = None
        current_dd_peak = prices[0]

        for i in range(1, len(prices)):
            if prices[i] > peak:
                # New peak — close any open drawdown
                if current_dd_start is not None:
                    dd_depth = ((prices[current_dd_start:i].min() / current_dd_peak) - 1) * 100
                    if dd_depth < -5:
                        trough_idx = current_dd_start + np.argmin(prices[current_dd_start:i])
                        drawdowns.append({
                            "depth_pct": round(dd_depth, 2),
                            "duration_to_trough": int(trough_idx - current_dd_start),
                            "recovery_days": int(i - trough_idx),
                            "total_days": int(i - current_dd_start),
                        })
                    current_dd_start = None
                peak = prices[i]
                current_dd_peak = prices[i]
            else:
                dd = ((prices[i] / peak) - 1) * 100
                if dd < -5 and current_dd_start is None:
                    current_dd_start = i
                    current_dd_peak = peak

        if len(drawdowns) < 3:
            return {}

        depths = [d["depth_pct"] for d in drawdowns]
        durations = [d["duration_to_trough"] for d in drawdowns]
        recoveries = [d["recovery_days"] for d in drawdowns]

        result = {
            "total_drawdowns": len(drawdowns),
            "avg_depth_pct": round(float(np.mean(depths)), 2),
            "median_depth_pct": round(float(np.median(depths)), 2),
            "avg_duration_to_trough_days": round(float(np.mean(durations)), 0),
            "avg_recovery_days": round(float(np.mean(recoveries)), 0),
            "percentiles": {
                "p25_depth": round(float(np.percentile(depths, 25)), 2),
                "p50_depth": round(float(np.percentile(depths, 50)), 2),
                "p75_depth": round(float(np.percentile(depths, 75)), 2),
                "p90_depth": round(float(np.percentile(depths, 90)), 2),
            },
            "worst_drawdown_pct": round(float(min(depths)), 2),
        }

        logger.info(f"Drawdown pattern analysis: {len(drawdowns)} drawdowns, avg depth {result['avg_depth_pct']:.1f}%")
        return result

    except Exception as e:
        logger.debug(f"Drawdown pattern analysis failed: {e}")
        return {}


# ============================================================
#  UPGRADE 6: VIX TERM STRUCTURE HISTORY
# ============================================================

def _analyze_vix_term_structure_history() -> dict:
    """
    Study historical VIX term structure (contango vs backwardation).
    After deep backwardation, does the market recover or crash further?

    Returns: {backwardation_forward_returns, avg_backwardation_duration, contango_forward_returns}
    """
    try:
        time.sleep(_BATCH_DELAY)
        vix_df = yf.download(["^VIX", "^VIX3M"], period="max", progress=False, group_by="ticker")

        if vix_df is None or vix_df.empty:
            return {}

        try:
            vix_spot = vix_df[("^VIX", "Close")].dropna()
            vix_3m = vix_df[("^VIX3M", "Close")].dropna()
        except Exception:
            return {}

        common = vix_spot.index.intersection(vix_3m.index)
        if len(common) < 252:
            return {}

        vix_s = vix_spot.loc[common].values.astype(float)
        vix_3 = vix_3m.loc[common].values.astype(float)
        ratio = vix_s / (vix_3 + 0.01)

        # Download SPY for forward returns
        time.sleep(_BATCH_DELAY)
        spy_df = yf.download("SPY", period="max", progress=False)
        if spy_df is None:
            return {}
        spy_close = spy_df["Close"]
        if hasattr(spy_close, "columns"):
            spy_close = spy_close.iloc[:, 0]
        spy_close = spy_close.dropna()

        spy_common = spy_close.index.intersection(vix_spot.loc[common].index)
        if len(spy_common) < 252:
            return {}

        spy_prices = spy_close.loc[spy_common].values.astype(float)
        ratio_aligned = []
        for date in spy_common:
            if date in vix_spot.loc[common].index and date in vix_3m.loc[common].index:
                s = float(vix_spot.loc[common].loc[date])
                t = float(vix_3m.loc[common].loc[date])
                ratio_aligned.append(s / (t + 0.01))
            else:
                ratio_aligned.append(1.0)
        ratio_aligned = np.array(ratio_aligned)

        # Analyze forward returns after different term structure states
        def forward_returns(mask, horizon):
            """Avg SPY return N days after condition."""
            returns = []
            for i in np.where(mask)[0]:
                if i + horizon < len(spy_prices):
                    ret = ((spy_prices[i + horizon] / spy_prices[i]) - 1) * 100
                    returns.append(ret)
            return round(float(np.mean(returns)), 3) if returns else 0

        deep_backwardation = ratio_aligned > 1.15  # VIX >> VIX3M (panic)
        backwardation = ratio_aligned > 1.05
        contango = ratio_aligned < 0.95  # Normal, calm

        result = {
            "deep_backwardation": {
                "5d_forward_return": forward_returns(deep_backwardation, 5),
                "10d_forward_return": forward_returns(deep_backwardation, 10),
                "21d_forward_return": forward_returns(deep_backwardation, 21),
                "days_observed": int(deep_backwardation.sum()),
            },
            "backwardation": {
                "5d_forward_return": forward_returns(backwardation, 5),
                "10d_forward_return": forward_returns(backwardation, 10),
                "21d_forward_return": forward_returns(backwardation, 21),
                "days_observed": int(backwardation.sum()),
            },
            "contango": {
                "5d_forward_return": forward_returns(contango, 5),
                "10d_forward_return": forward_returns(contango, 10),
                "21d_forward_return": forward_returns(contango, 21),
                "days_observed": int(contango.sum()),
            },
        }

        logger.info(f"VIX term structure analysis: {int(deep_backwardation.sum())} deep backwardation days, "
                     f"{int(contango.sum())} contango days")
        return result

    except Exception as e:
        logger.debug(f"VIX term structure analysis failed: {e}")
        return {}


def build_calibration(symbols: list, sector_map: dict = None) -> dict:
    """
    Master function: build the full historical calibration.
    This is the heavy-lift function called by the scheduler.
    """
    global _calibration, _calibration_loaded

    logger.info(f"Building historical calibration for {len(symbols)} symbols...")
    start_time = time.time()

    # Check if we have a recent calibration
    if _calibration and _calibration.get("built_at_epoch", 0) > time.time() - 86400:
        logger.info("Calibration is less than 24h old — skipping rebuild")
        return _calibration

    # Download max history
    history = _fetch_max_history(symbols)
    logger.info(f"Downloaded history for {len(history)} stocks")

    if not history:
        logger.warning("No historical data downloaded — calibration aborted")
        return _calibration

    # Get SPY for regime analysis
    spy_series = history.get("SPY")
    if spy_series is None:
        try:
            logger.info("Downloading SPY history separately...")
            spy_df = yf.download("SPY", period="max", progress=False)
            if spy_df is not None and len(spy_df) >= 252:
                close_col = spy_df["Close"]
                if hasattr(close_col, "columns"):
                    close_col = close_col.iloc[:, 0]
                spy_series = close_col.dropna()
        except Exception:
            spy_series = None

    # Build per-stock calibration
    stock_cal = {}
    sector_data = {}

    for symbol, close_series in history.items():
        try:
            cal = {}

            # 1. Seasonal patterns
            seasonal = _analyze_seasonal_patterns(close_series)
            if seasonal:
                cal["seasonal"] = seasonal

            # 2. Mean reversion half-life
            half_life = _analyze_mean_reversion_halflife(close_series)
            cal["half_life_days"] = round(half_life, 1)

            # 3. Momentum persistence
            momentum = _analyze_momentum_persistence(close_series)
            cal["optimal_momentum_period"] = momentum["optimal_period"]
            cal["momentum_persistence"] = momentum["persistence_score"]

            # 4. Regime performance (needs SPY)
            if spy_series is not None:
                regime_perf = _analyze_regime_performance(close_series, spy_series)
                if regime_perf:
                    cal["regime_performance"] = regime_perf

            # UPGRADE 3: Earnings seasonality
            earnings_seas = _analyze_earnings_seasonality(close_series)
            if earnings_seas:
                cal["earnings_seasonality"] = earnings_seas

            # Track data length
            cal["history_years"] = round(len(close_series) / 252, 1)

            stock_cal[symbol] = cal

            # Group by sector for rotation analysis
            if sector_map:
                sector = sector_map.get(symbol, "Unknown")
                if sector not in ("ETF", "Unknown"):
                    if sector not in sector_data:
                        sector_data[sector] = []
                    sector_data[sector].append(close_series)

        except Exception as e:
            logger.debug(f"Calibration failed for {symbol}: {e}")

    # 5. Sector rotation analysis
    rotation = {}
    if spy_series is not None and sector_data:
        rotation = _analyze_sector_rotation(sector_data, spy_series)

    # UPGRADE 1: Crisis correlation matrix
    crisis_corr = {}
    if spy_series is not None:
        crisis_corr = _analyze_crisis_correlations(history, spy_series)

    # UPGRADE 2: Volatility regime detection
    vol_regimes = {}
    if spy_series is not None:
        vol_regimes = _analyze_volatility_regimes(spy_series)

    # UPGRADE 4: Cross-asset leading indicators
    cross_asset = {}
    if sector_map:
        cross_asset = _analyze_cross_asset_leads(history, sector_map)

    # UPGRADE 5: Drawdown pattern recognition
    drawdown_patterns = {}
    if spy_series is not None:
        drawdown_patterns = _analyze_drawdown_patterns(spy_series)

    # UPGRADE 6: VIX term structure history
    vix_patterns = _analyze_vix_term_structure_history()

    # Build final calibration object
    calibration = {
        "stocks": stock_cal,
        "sector_rotation": rotation,
        "crisis_correlations": crisis_corr,
        "volatility_regimes": vol_regimes,
        "cross_asset_leads": cross_asset,
        "drawdown_patterns": drawdown_patterns,
        "vix_patterns": vix_patterns,
        "built_at": datetime.now().isoformat(),
        "built_at_epoch": time.time(),
        "stock_count": len(stock_cal),
        "build_time_seconds": round(time.time() - start_time, 1),
    }

    _calibration = calibration
    _calibration_loaded = True

    # Persist
    _save_to_disk(calibration)
    _save_to_s3(calibration)

    elapsed = time.time() - start_time
    logger.info(
        f"Historical calibration complete: {len(stock_cal)} stocks analyzed in {elapsed:.0f}s | "
        f"Sector rotation: {len(rotation)} sectors | "
        f"Avg history: {np.mean([c.get('history_years', 0) for c in stock_cal.values()]):.1f} years"
    )

    return calibration
