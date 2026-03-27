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
def _market_ttl():
    """60s cache during market hours, 15min otherwise"""
    from datetime import datetime
    now = datetime.now()
    t = now.hour * 60 + now.minute
    return 60 if (390 <= t <= 1050) else 900

_extras_cache_ttl = 60  # Default, overridden by _market_ttl()
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
        ttl = _market_ttl()
    now = time.time()
    if key in _extras_cache and now - _extras_cache[key]["time"] < ttl:
        return _extras_cache[key]["data"]
    data = fetch_fn()
    _extras_cache[key] = {"data": data, "time": now}
    return data


# --- Banner tickers ---

BANNER_SYMBOLS = [
    # Major Indexes
    ("^GSPC", "S&P 500"),
    ("^IXIC", "Nasdaq"),
    ("^DJI", "Dow Jones"),
    # Commodities & Bonds
    ("GC=F", "Gold"),
    ("CL=F", "Crude Oil"),
    ("^TNX", "10Y Treasury"),
    # Top stocks by market cap
    ("AAPL", "Apple"),
    ("MSFT", "Microsoft"),
    ("NVDA", "NVIDIA"),
    ("AMZN", "Amazon"),
    ("GOOGL", "Alphabet"),
    ("META", "Meta"),
    ("TSLA", "Tesla"),
    ("BRK-B", "Berkshire"),
    ("AVGO", "Broadcom"),
    ("JPM", "JPMorgan"),
    ("LLY", "Eli Lilly"),
    ("V", "Visa"),
    ("UNH", "UnitedHealth"),
    ("WMT", "Walmart"),
    ("XOM", "Exxon"),
    ("NFLX", "Netflix"),
    ("AMD", "AMD"),
    ("CRM", "Salesforce"),
    ("COST", "Costco"),
    ("BA", "Boeing"),
    ("DIS", "Disney"),
    ("COIN", "Coinbase"),
]


def get_banner_data():
    """Get current prices and daily changes for banner tickers."""
    def fetch():
        results = []
        # Download all at once to minimize API calls
        _throttle()
        symbols = [s[0] for s in BANNER_SYMBOLS]
        symbol_to_name = {s[0]: s[1] for s in BANNER_SYMBOLS}

        try:
            df = yf.download(symbols, period="5d", progress=False, group_by="ticker")
        except Exception:
            return []

        if df is None or df.empty:
            return []

        for symbol in symbols:
            try:
                name = symbol_to_name[symbol]

                # Handle both multi-level and flat column structures
                if isinstance(df.columns, pd.MultiIndex):
                    # Multi-ticker download: columns are (Ticker, Price)
                    if symbol in df.columns.get_level_values(0):
                        close_series = df[(symbol, "Close")].dropna()
                    else:
                        continue
                else:
                    # Single ticker or flat structure
                    if "Close" in df.columns:
                        close_series = df["Close"].dropna()
                    else:
                        continue

                if close_series is None or len(close_series) < 2:
                    continue

                current = float(close_series.iloc[-1])
                prev = float(close_series.iloc[-2])
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


# --- Sector Heatmap ---

SECTOR_ETFS = [
    ("XLK", "Technology"),
    ("XLF", "Financials"),
    ("XLV", "Healthcare"),
    ("XLE", "Energy"),
    ("XLY", "Consumer Disc."),
    ("XLP", "Consumer Staples"),
    ("XLI", "Industrials"),
    ("XLB", "Materials"),
    ("XLRE", "Real Estate"),
    ("XLU", "Utilities"),
    ("XLC", "Communication"),
]


def get_sector_heatmap():
    """Get today's performance for each S&P 500 sector via SPDR ETFs."""
    def fetch():
        symbols = [s[0] for s in SECTOR_ETFS]
        symbol_to_name = {s[0]: s[1] for s in SECTOR_ETFS}
        _throttle()
        try:
            df = yf.download(symbols, period="5d", progress=False, group_by="ticker")
        except Exception:
            return {"sectors": [], "error": "Could not fetch sector data"}

        if df is None or df.empty:
            return {"sectors": [], "error": "No data"}

        sectors = []
        for symbol in symbols:
            try:
                if isinstance(df.columns, pd.MultiIndex):
                    if symbol not in df.columns.get_level_values(0):
                        continue
                    close_series = df[(symbol, "Close")].dropna()
                else:
                    continue

                if close_series is None or len(close_series) < 2:
                    continue

                current = float(close_series.iloc[-1])
                prev = float(close_series.iloc[-2])
                change_pct = ((current / prev) - 1) * 100

                sectors.append({
                    "symbol": symbol,
                    "name": symbol_to_name[symbol],
                    "price": round(current, 2),
                    "change_pct": round(change_pct, 2),
                })
            except Exception:
                continue

        # Sort by performance
        sectors.sort(key=lambda x: x["change_pct"], reverse=True)
        return {
            "sectors": sectors,
            "generated_at": datetime.now().isoformat(),
        }

    return _get_cached("sector_heatmap", fetch, ttl=300)


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
    Symbols to Buy — hedge fund grade stock screening.
    Uses EMA crossovers, RSI, MACD, pivot points, and momentum.
    Calculates recommended hold duration and entry/exit timing.
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
                series = pd.Series(closes)

                # RSI (Wilder's smoothing)
                deltas = np.diff(closes)
                gains = np.where(deltas > 0, deltas, 0)
                losses = np.where(deltas < 0, -deltas, 0)
                avg_gain = np.mean(gains[-14:])
                avg_loss = np.mean(losses[-14:])
                rs = avg_gain / avg_loss if avg_loss > 0 else 100
                rsi = 100 - (100 / (1 + rs))

                # EMA crossovers (9, 21, 50)
                ema_9 = float(series.ewm(span=9, adjust=False).mean().iloc[-1])
                ema_21 = float(series.ewm(span=21, adjust=False).mean().iloc[-1])
                ema_50 = float(series.ewm(span=50, adjust=False).mean().iloc[-1]) if len(closes) >= 50 else ema_21

                # MACD
                ema_12 = series.ewm(span=12, adjust=False).mean()
                ema_26 = series.ewm(span=26, adjust=False).mean()
                macd_line = float((ema_12 - ema_26).iloc[-1])
                signal_line = float((ema_12 - ema_26).ewm(span=9, adjust=False).mean().iloc[-1])
                macd_bullish = macd_line > signal_line

                # Momentum (20-day return)
                momentum = ((closes[-1] / closes[-20]) - 1) * 100 if len(closes) >= 20 else 0

                # Volatility
                log_returns = np.log(closes[1:] / closes[:-1])
                vol = float(np.std(log_returns)) * np.sqrt(252) * 100

                # Pivot points
                high_20 = max(closes[-20:])
                low_20 = min(closes[-20:])
                pivot = (high_20 + low_20 + current) / 3
                above_pivot = current > pivot

                # --- Multi-factor scoring ---
                score = 0
                signal = "Hold"
                reasons = []
                action = "Hold"

                # RSI factor
                if rsi < 25:
                    score += 3
                    reasons.append(f"Deeply oversold (RSI {rsi:.0f})")
                elif rsi < 35:
                    score += 2
                    reasons.append(f"Oversold zone (RSI {rsi:.0f})")
                elif rsi > 75:
                    score -= 3
                    reasons.append(f"Extremely overbought (RSI {rsi:.0f})")
                elif rsi > 65:
                    score -= 1
                    reasons.append(f"Overbought (RSI {rsi:.0f})")

                # EMA alignment factor
                if current > ema_9 > ema_21 > ema_50:
                    score += 3
                    reasons.append("Perfect bullish EMA stack")
                elif current > ema_9 > ema_21:
                    score += 2
                    reasons.append("Bullish EMA alignment")
                elif current < ema_9 < ema_21 < ema_50:
                    score -= 2
                    reasons.append("Bearish EMA alignment")

                # MACD factor
                if macd_bullish:
                    score += 1
                    reasons.append("MACD bullish crossover")
                else:
                    score -= 1

                # Momentum factor
                if momentum > 8:
                    score += 2
                    reasons.append(f"Strong momentum (+{momentum:.1f}%)")
                elif momentum > 2:
                    score += 1
                    reasons.append(f"Positive momentum (+{momentum:.1f}%)")
                elif momentum < -8:
                    score -= 2
                    reasons.append(f"Weak momentum ({momentum:.1f}%)")

                # Pivot point factor
                if above_pivot:
                    score += 1
                    reasons.append("Trading above pivot point")
                else:
                    score -= 1

                # Volatility factor
                if vol < 25:
                    score += 1
                elif vol > 50:
                    score -= 1
                    reasons.append(f"High volatility risk ({vol:.0f}%)")

                # Determine action and signal
                if score >= 5:
                    signal = "Strong Buy"
                    action = "Buy Now"
                elif score >= 3:
                    signal = "Strong Buy"
                    action = "Buy"
                elif score >= 1:
                    signal = "Buy"
                    action = "Buy"
                elif score <= -3:
                    signal = "Strong Sell"
                    action = "Sell"
                elif score <= -1:
                    signal = "Sell"
                    action = "Sell"
                else:
                    action = "Hold"

                # Hold duration calculation (simplified for picks)
                hold_days = 14  # base 2 weeks
                if rsi < 30:
                    hold_days += 21  # oversold: hold for recovery
                if current > ema_9 > ema_21 > ema_50:
                    hold_days += 14  # strong trend: ride it
                if vol > 40:
                    hold_days = max(7, hold_days - 7)  # high vol: shorter
                if vol < 20:
                    hold_days += 14  # low vol: safe to hold longer
                hold_days = max(7, min(90, hold_days))

                if hold_days <= 10:
                    hold_label = "1-2 Weeks"
                elif hold_days <= 21:
                    hold_label = "2-3 Weeks"
                elif hold_days <= 35:
                    hold_label = "1 Month"
                elif hold_days <= 60:
                    hold_label = "1-2 Months"
                else:
                    hold_label = "2-3 Months"

                # Probability estimate
                daily_mean = float(np.mean(log_returns))
                daily_std = float(np.std(log_returns))
                tf_mean = daily_mean * 30
                tf_std = daily_std * np.sqrt(30)
                prob_up_30d = float(1 - norm.cdf(0, loc=tf_mean, scale=tf_std)) * 100

                # Entry price guidance
                entry = "At market" if score >= 3 else f"Near ${round(ema_21, 2)}" if score >= 1 else "Avoid"
                target = round(current * (1 + (prob_up_30d / 100) * 0.1), 2) if action == "Buy" or action == "Buy Now" else round(current * 0.95, 2)
                stop_loss = round(current * 0.95, 2) if action != "Sell" else None

                picks.append({
                    "rank": 0,
                    "symbol": symbol,
                    "price": round(current, 2),
                    "rsi": round(rsi, 1),
                    "momentum_20d": round(momentum, 2),
                    "volatility": round(vol, 1),
                    "signal": signal,
                    "action": action,
                    "score": score,
                    "prob_up_30d": round(prob_up_30d, 1),
                    "hold_days": hold_days,
                    "hold_label": hold_label,
                    "entry": entry,
                    "target": target,
                    "stop_loss": stop_loss,
                    "ema_9": round(ema_9, 2),
                    "ema_21": round(ema_21, 2),
                    "pivot": round(pivot, 2),
                    "reasons": reasons[:4],
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


# --- Daily AI Summary ---

SUMMARY_STOCKS = [s[0] for s in BANNER_SYMBOLS if not s[0].startswith("^")]


def get_daily_summary(watchlist_tickers=None):
    """
    Daily AI Summary — top gainers, biggest losers among S&P 500 big caps,
    plus watchlist summary for the user's stocks.
    Cached for 5 minutes during market hours, 15 min otherwise.
    """
    def fetch():
        _throttle()
        try:
            df = yf.download(SUMMARY_STOCKS, period="5d", progress=False, group_by="ticker")
        except Exception:
            return {"error": "Could not fetch market data", "gainers": [], "losers": [], "watchlist_summary": []}

        if df is None or df.empty:
            return {"error": "No data available", "gainers": [], "losers": [], "watchlist_summary": []}

        symbol_to_name = {s[0]: s[1] for s in BANNER_SYMBOLS}
        movers = []

        for symbol in SUMMARY_STOCKS:
            try:
                if isinstance(df.columns, pd.MultiIndex):
                    if symbol not in df.columns.get_level_values(0):
                        continue
                    close_series = df[(symbol, "Close")].dropna()
                else:
                    continue

                if close_series is None or len(close_series) < 2:
                    continue

                current = float(close_series.iloc[-1])
                prev = float(close_series.iloc[-2])
                change = current - prev
                change_pct = (change / prev) * 100

                movers.append({
                    "symbol": symbol,
                    "name": symbol_to_name.get(symbol, symbol),
                    "price": round(current, 2),
                    "change": round(change, 2),
                    "change_pct": round(change_pct, 2),
                })
            except Exception:
                continue

        # Sort for gainers and losers
        movers.sort(key=lambda x: x["change_pct"], reverse=True)
        gainers = movers[:10]
        losers = sorted(movers, key=lambda x: x["change_pct"])[:10]

        # Market overview
        total_up = sum(1 for m in movers if m["change_pct"] > 0)
        total_down = sum(1 for m in movers if m["change_pct"] < 0)
        avg_change = sum(m["change_pct"] for m in movers) / len(movers) if movers else 0

        if avg_change > 0.5:
            market_mood = "Bullish"
        elif avg_change > 0:
            market_mood = "Slightly Bullish"
        elif avg_change > -0.5:
            market_mood = "Slightly Bearish"
        else:
            market_mood = "Bearish"

        result = {
            "gainers": gainers,
            "losers": losers,
            "market_overview": {
                "total_stocks": len(movers),
                "advancing": total_up,
                "declining": total_down,
                "avg_change_pct": round(avg_change, 2),
                "mood": market_mood,
            },
            "generated_at": datetime.now().isoformat(),
        }

        return result

    summary = _get_cached("daily_summary", fetch, ttl=300)

    # Add watchlist summary if tickers provided
    if watchlist_tickers:
        wl_tickers = [t.strip().upper() for t in watchlist_tickers.split(",") if t.strip()]
        if wl_tickers:
            wl_summary = _get_watchlist_summary(wl_tickers)
            summary = dict(summary)  # copy so we don't mutate cache
            summary["watchlist_summary"] = wl_summary

    return summary


def _get_watchlist_summary(tickers):
    """Get quick summary for user's watchlist stocks."""
    results = []
    _throttle()
    try:
        df = yf.download(tickers, period="1mo", progress=False, group_by="ticker")
    except Exception:
        return []

    if df is None or df.empty:
        return []

    for symbol in tickers:
        try:
            if isinstance(df.columns, pd.MultiIndex):
                if symbol not in df.columns.get_level_values(0):
                    continue
                close_series = df[(symbol, "Close")].dropna()
            elif len(tickers) == 1:
                close_series = df["Close"].dropna()
            else:
                continue

            if close_series is None or len(close_series) < 2:
                continue

            closes = close_series.values.astype(float)
            current = closes[-1]
            prev = closes[-2]
            day_change = ((current / prev) - 1) * 100

            # Week change
            week_change = ((current / closes[-5]) - 1) * 100 if len(closes) >= 5 else day_change

            # Month change
            month_change = ((current / closes[0]) - 1) * 100

            # Simple RSI
            deltas = np.diff(closes[-15:])
            gains = np.where(deltas > 0, deltas, 0)
            losses = np.where(deltas < 0, -deltas, 0)
            avg_gain = np.mean(gains)
            avg_loss = np.mean(losses)
            rs = avg_gain / avg_loss if avg_loss > 0 else 100
            rsi = 100 - (100 / (1 + rs))

            # Simple signal
            if rsi < 30:
                signal = "Oversold - Consider Buying"
            elif rsi > 70:
                signal = "Overbought - Consider Selling"
            elif day_change > 2:
                signal = "Strong Day - Monitor"
            elif day_change < -2:
                signal = "Down Day - Watch Support"
            else:
                signal = "Neutral - Hold"

            results.append({
                "symbol": symbol,
                "price": round(current, 2),
                "day_change_pct": round(day_change, 2),
                "week_change_pct": round(week_change, 2),
                "month_change_pct": round(month_change, 2),
                "rsi": round(rsi, 1),
                "signal": signal,
            })
        except Exception:
            continue

    return results
