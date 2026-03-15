"""
Extra Resources — provides data for the stock ticker banner,
daily top picks, and upcoming earnings calendar.

Uses yfinance carefully with throttling. Caches aggressively
to avoid hitting Yahoo Finance rate limits.
"""

import yfinance as yf
import time
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from scipy.stats import norm

# Shared cache
_extras_cache = {}
_extras_cache_ttl = 900  # 15 minutes for extras (less frequent updates OK)
_last_api_call = [0.0]
_API_DELAY = 3.0


def _throttle():
    now = time.time()
    elapsed = now - _last_api_call[0]
    if elapsed < _API_DELAY:
        time.sleep(_API_DELAY - elapsed)
    _last_api_call[0] = time.time()


def _get_cached(key, fetch_fn, ttl=None):
    if ttl is None:
        ttl = _extras_cache_ttl
    now = time.time()
    if key in _extras_cache and now - _extras_cache[key]["time"] < ttl:
        return _extras_cache[key]["data"]
    data = fetch_fn()
    _extras_cache[key] = {"data": data, "time": now}
    return data


# --- Banner tickers ---

BANNER_SYMBOLS = [
    ("^GSPC", "S&P 500"),
    ("^IXIC", "Nasdaq"),
    ("^DJI", "Dow Jones"),
    ("AAPL", "Apple"),
    ("TSLA", "Tesla"),
    ("NVDA", "NVIDIA"),
    ("MSFT", "Microsoft"),
    ("AMZN", "Amazon"),
    ("GOOGL", "Google"),
    ("META", "Meta"),
    ("SBUX", "Starbucks"),
    ("JPM", "JPMorgan"),
    ("XOM", "Exxon"),
    ("DIS", "Disney"),
    ("NFLX", "Netflix"),
    ("BA", "Boeing"),
    ("NKE", "Nike"),
    ("COIN", "Coinbase"),
    ("AMD", "AMD"),
    ("COST", "Costco"),
]


def get_banner_data():
    """Get current prices and daily changes for banner tickers."""
    def fetch():
        results = []
        # Download all at once to minimize API calls
        _throttle()
        symbols = [s[0] for s in BANNER_SYMBOLS]
        try:
            df = yf.download(symbols, period="2d", progress=False, group_by="ticker")
        except Exception:
            return []

        if df is None or df.empty:
            return []

        for symbol, name in BANNER_SYMBOLS:
            try:
                if len(BANNER_SYMBOLS) == 1:
                    ticker_df = df
                else:
                    ticker_df = df[symbol] if symbol in df.columns.get_level_values(0) else None

                if ticker_df is None or ticker_df.empty or len(ticker_df) < 2:
                    continue

                current = float(ticker_df["Close"].iloc[-1])
                prev = float(ticker_df["Close"].iloc[-2])
                change = current - prev
                change_pct = (change / prev) * 100

                results.append({
                    "symbol": symbol,
                    "name": name,
                    "price": round(current, 2),
                    "change": round(change, 2),
                    "change_pct": round(change_pct, 2),
                })
            except Exception:
                continue

        return results

    return _get_cached("banner_data", fetch)


# --- Top 15 Daily Picks ---

PICK_CANDIDATES = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "NFLX",
    "JPM", "V", "MA", "UNH", "JNJ", "PG", "HD",
    "CRM", "ADBE", "AMD", "INTC", "QCOM",
    "PFE", "ABBV", "MRK", "LLY", "BMY",
    "XOM", "CVX", "COP", "SLB",
    "BA", "CAT", "HON", "GE",
    "DIS", "CMCSA", "NFLX", "SBUX", "MCD", "NKE",
    "WMT", "COST", "TGT",
    "GS", "MS", "BAC", "WFC", "C",
    "NEE", "DUK", "SO",
    "AMT", "PLD", "SPG",
]


def get_daily_picks():
    """
    Generate top 15 daily picks based on technical signals.
    Uses RSI, momentum, and volatility to rank stocks.
    Cached for 1 hour.
    """
    def fetch():
        picks = []

        # Download all candidates at once (1 API call)
        _throttle()
        try:
            df = yf.download(PICK_CANDIDATES, period="3mo", progress=False, group_by="ticker")
        except Exception:
            return {"picks": [], "generated_at": datetime.now().isoformat(), "error": "Could not fetch data"}

        if df is None or df.empty:
            return {"picks": [], "generated_at": datetime.now().isoformat(), "error": "No data available"}

        for symbol in PICK_CANDIDATES:
            try:
                ticker_df = df[symbol] if symbol in df.columns.get_level_values(0) else None
                if ticker_df is None or ticker_df.empty or len(ticker_df) < 30:
                    continue

                closes = ticker_df["Close"].dropna().values.astype(float)
                if len(closes) < 30:
                    continue

                current = closes[-1]

                # RSI (Wilder's smoothing)
                deltas = np.diff(closes)
                gains = np.where(deltas > 0, deltas, 0)
                losses = np.where(deltas < 0, -deltas, 0)
                avg_gain = np.mean(gains[-14:])
                avg_loss = np.mean(losses[-14:])
                rs = avg_gain / avg_loss if avg_loss > 0 else 100
                rsi = 100 - (100 / (1 + rs))

                # Momentum (20-day return)
                if len(closes) >= 20:
                    momentum = ((closes[-1] / closes[-20]) - 1) * 100
                else:
                    momentum = 0

                # Volatility
                log_returns = np.log(closes[1:] / closes[:-1])
                vol = float(np.std(log_returns)) * np.sqrt(252) * 100

                # Trend (price vs 20 SMA)
                sma20 = np.mean(closes[-20:])
                above_sma = current > sma20

                # Score: favor oversold (low RSI), positive momentum, reasonable vol
                score = 0
                signal = "Hold"
                reasons = []

                if rsi < 30:
                    score += 3
                    reasons.append(f"Oversold (RSI {rsi:.0f})")
                elif rsi < 40:
                    score += 2
                    reasons.append(f"Near oversold (RSI {rsi:.0f})")
                elif rsi > 70:
                    score -= 2
                    reasons.append(f"Overbought (RSI {rsi:.0f})")
                elif rsi > 60:
                    score -= 1

                if momentum > 5:
                    score += 2
                    reasons.append(f"Strong momentum (+{momentum:.1f}%)")
                elif momentum > 0:
                    score += 1
                    reasons.append(f"Positive momentum (+{momentum:.1f}%)")
                elif momentum < -5:
                    score -= 1
                    reasons.append(f"Weak momentum ({momentum:.1f}%)")

                if above_sma:
                    score += 1
                    reasons.append("Above 20-day average")
                else:
                    score -= 1
                    reasons.append("Below 20-day average")

                if vol < 25:
                    score += 1
                    reasons.append(f"Low volatility ({vol:.0f}%)")
                elif vol > 50:
                    score -= 1
                    reasons.append(f"High volatility ({vol:.0f}%)")

                if score >= 3:
                    signal = "Strong Buy"
                elif score >= 1:
                    signal = "Buy"
                elif score <= -3:
                    signal = "Strong Sell"
                elif score <= -1:
                    signal = "Sell"

                # Probability estimate (simplified)
                daily_mean = float(np.mean(log_returns))
                daily_std = float(np.std(log_returns))
                tf_mean = daily_mean * 30
                tf_std = daily_std * np.sqrt(30)
                prob_up_30d = float(1 - norm.cdf(0, loc=tf_mean, scale=tf_std)) * 100

                picks.append({
                    "rank": 0,
                    "symbol": symbol,
                    "price": round(current, 2),
                    "rsi": round(rsi, 1),
                    "momentum_20d": round(momentum, 2),
                    "volatility": round(vol, 1),
                    "signal": signal,
                    "score": score,
                    "prob_up_30d": round(prob_up_30d, 1),
                    "reasons": reasons[:3],
                })
            except Exception:
                continue

        # Sort by score descending, take top 15
        picks.sort(key=lambda x: x["score"], reverse=True)
        top_15 = picks[:15]
        for i, p in enumerate(top_15):
            p["rank"] = i + 1

        return {
            "picks": top_15,
            "generated_at": datetime.now().isoformat(),
            "total_analyzed": len(picks),
        }

    return _get_cached("daily_picks", fetch, ttl=3600)  # 1 hour cache


# --- Earnings Calendar ---


def get_earnings_calendar():
    """
    Get upcoming earnings for major stocks in the next 14 days.
    Uses a fast batch approach: checks a small set of high-priority stocks
    with earnings_dates instead of calendar (more reliable).
    Also extends to 14 days for better coverage.
    Cached for 6 hours.
    """
    def fetch():
        today = datetime.now().date()
        week_end = today + timedelta(days=14)

        # Only check ~15 stocks at a time to stay fast (15 × 3s = 45s max)
        # These are the biggest market-moving earnings reporters
        priority_stocks = [
            ("AAPL", "Apple Inc"),
            ("MSFT", "Microsoft"),
            ("GOOGL", "Alphabet"),
            ("AMZN", "Amazon"),
            ("META", "Meta Platforms"),
            ("NVDA", "NVIDIA"),
            ("TSLA", "Tesla"),
            ("NFLX", "Netflix"),
            ("JPM", "JPMorgan Chase"),
            ("BAC", "Bank of America"),
            ("UNH", "UnitedHealth"),
            ("JNJ", "Johnson & Johnson"),
            ("XOM", "Exxon Mobil"),
            ("WMT", "Walmart"),
            ("HD", "Home Depot"),
            ("NKE", "Nike"),
            ("FDX", "FedEx"),
            ("MU", "Micron Technology"),
            ("ADBE", "Adobe"),
            ("CRM", "Salesforce"),
            ("COST", "Costco"),
            ("DIS", "Walt Disney"),
            ("BA", "Boeing"),
            ("GS", "Goldman Sachs"),
            ("V", "Visa"),
        ]

        upcoming = []

        for symbol, name in priority_stocks:
            try:
                _throttle()
                stock = yf.Ticker(symbol)

                # Try earnings_dates first (more reliable than calendar)
                try:
                    ed_df = stock.earnings_dates
                    if ed_df is not None and not ed_df.empty:
                        for idx in ed_df.index:
                            try:
                                if hasattr(idx, 'date'):
                                    ed = idx.date()
                                else:
                                    ed = pd.Timestamp(idx).date()

                                if today <= ed <= week_end:
                                    eps_est = None
                                    rev_est = None
                                    try:
                                        if "EPS Estimate" in ed_df.columns:
                                            val = ed_df.loc[idx, "EPS Estimate"]
                                            if pd.notna(val):
                                                eps_est = round(float(val), 2)
                                    except Exception:
                                        pass

                                    upcoming.append({
                                        "symbol": symbol,
                                        "name": name,
                                        "date": ed.isoformat(),
                                        "day_of_week": ed.strftime("%A"),
                                        "eps_estimate": eps_est,
                                        "revenue_estimate": rev_est,
                                    })
                                    break  # Only need the next earnings date
                            except Exception:
                                continue
                        continue  # Move to next stock
                except Exception:
                    pass

                # Fallback: try calendar
                try:
                    cal = stock.calendar
                    if cal and isinstance(cal, dict):
                        ed_raw = cal.get("Earnings Date")
                        if ed_raw:
                            if isinstance(ed_raw, list) and len(ed_raw) > 0:
                                ed_raw = ed_raw[0]
                            if hasattr(ed_raw, 'date'):
                                ed = ed_raw.date()
                            elif isinstance(ed_raw, str):
                                ed = datetime.strptime(ed_raw[:10], "%Y-%m-%d").date()
                            else:
                                continue

                            if today <= ed <= week_end:
                                upcoming.append({
                                    "symbol": symbol,
                                    "name": name,
                                    "date": ed.isoformat(),
                                    "day_of_week": ed.strftime("%A"),
                                    "eps_estimate": round(float(cal.get("Earnings Average", 0)), 2) if cal.get("Earnings Average") else None,
                                    "revenue_estimate": None,
                                })
                except Exception:
                    continue

            except Exception:
                continue

        # Sort by date
        upcoming.sort(key=lambda x: x["date"])

        return {
            "earnings": upcoming,
            "week_start": today.isoformat(),
            "week_end": week_end.isoformat(),
            "stocks_checked": len(priority_stocks),
            "generated_at": datetime.now().isoformat(),
        }

    return _get_cached("earnings_calendar", fetch, ttl=21600)  # 6 hour cache
