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

    # Build final calibration object
    calibration = {
        "stocks": stock_cal,
        "sector_rotation": rotation,
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
