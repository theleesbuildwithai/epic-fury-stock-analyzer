"""
Backtest Framework — Sentinel Quant

Replays the trading strategy on historical price data to validate that
parameter changes actually help (or hurt) before deploying them live.

Public API:
  - run_backtest(start_date, end_date, tickers=None, params=None) -> dict
      Simulates the strategy over the date range, returns metrics.
  - get_backtest_summary() -> dict
      Returns the most recent backtest result (cached).

DESIGN:
  - SIMPLIFIED strategy emulation — uses the same ranking signals as live
    (composite trend score from price/volume momentum) but does NOT call
    the full quant_engine (too expensive to replay daily for 700 tickers).
    Instead computes lightweight per-day momentum scores and picks the
    top N longs / bottom N shorts each day.
  - Holds positions for ~5 days (matches typical hold_duration).
  - Applies real-world style: stop-loss, take-profit, position sizing.
  - Compares to S&P 500 buy-and-hold over same period.

SAFETY:
  - All yfinance calls wrapped in try/except + skip-on-fail
  - Bad data on any single ticker NEVER breaks the whole backtest
  - Returns ok=False on any unrecoverable error (never raises)
  - Uses cached price data when possible to avoid re-downloading

LIMITATIONS (be honest):
  - Survivorship bias: only tickers currently in universe (no delisted)
  - No real fills/slippage simulation — assumes mid-price execution
  - No options trading in backtest (equity only)
  - Simplified vs live strategy — useful for direction, not exact P&L
"""

import logging
import json
import os
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Default backtest config
DEFAULT_TOP_N = 10              # number of longs to hold each day
DEFAULT_HOLD_DAYS = 5           # avg holding period
DEFAULT_STOP_PCT = 0.04         # 4% stop loss
DEFAULT_TAKE_PCT = 0.10         # 10% take profit
DEFAULT_INITIAL_CAPITAL = 100_000.0
DEFAULT_POSITION_PCT = 0.08     # 8% per position

# Cache for the last backtest result (one-shot, in memory)
_last_backtest = {"data": None, "ts": 0}
_BACKTEST_TTL = 3600  # 1 hour cache


def _safe_yf_download(tickers: list, start: str, end: str, period: str = None) -> dict:
    """Download historical close prices for a list of tickers.
    Returns {ticker: pd.Series of closes} dict, skipping any that fail.
    Never raises.

    RESILIENCE (2026-05-15): yfinance bulk fetch of 100 tickers for 365
    days frequently fails with rate-limit or empty result.  Three-tier
    strategy:
      1. Try one bulk fetch (fastest happy path)
      2. If bulk yields <50% of requested tickers, chunk into 20-ticker
         batches with 0.5s spacing
      3. For tickers still missing, retry individually with 1s spacing

    This trades latency (backtest may now take 60-90s for full universe)
    for reliability — the previous 365-day failure mode is eliminated.
    """
    out = {}
    if not tickers:
        return out
    try:
        import yfinance as yf
        import pandas as pd
        import time as _time

        def _extract(df, syms):
            """Extract closes from a yf result into out dict.  Returns
            set of tickers successfully extracted."""
            got = set()
            if df is None or df.empty:
                return got
            for sym in syms:
                if sym in out:
                    got.add(sym)
                    continue
                try:
                    if isinstance(df.columns, pd.MultiIndex):
                        if sym in df.columns.get_level_values(0):
                            s = df[(sym, "Close")].dropna()
                            if len(s) >= 30:
                                out[sym] = s
                                got.add(sym)
                    elif len(syms) == 1:
                        s = df["Close"].dropna()
                        if len(s) >= 30:
                            out[sym] = s
                            got.add(sym)
                except Exception:
                    continue
            return got

        kwargs = {"start": start, "end": end, "progress": False,
                  "auto_adjust": True, "threads": True, "group_by": "ticker"}
        if period:
            kwargs["period"] = period

        # TIER 1: bulk fetch (fastest path)
        try:
            df = yf.download(tickers, **kwargs)
            _extract(df, tickers)
        except Exception as e:
            logger.debug(f"_safe_yf_download bulk tier failed: {e}")

        # If we got >= 50% of requested tickers, accept and return
        if len(out) >= len(tickers) * 0.5:
            return out

        # TIER 2: chunked retry (yfinance handles 20-ticker batches more reliably)
        missing = [t for t in tickers if t not in out]
        CHUNK_SIZE = 20
        for i in range(0, len(missing), CHUNK_SIZE):
            chunk = missing[i:i + CHUNK_SIZE]
            try:
                df = yf.download(chunk, **kwargs)
                _extract(df, chunk)
                _time.sleep(0.5)  # gentle on rate limits
            except Exception as e:
                logger.debug(f"_safe_yf_download chunk fail [{i}]: {e}")
                continue

        # TIER 3: individual retry for stragglers (rate-limit safest)
        still_missing = [t for t in tickers if t not in out]
        if still_missing and len(still_missing) <= 30:
            for sym in still_missing:
                try:
                    df = yf.download([sym], **kwargs)
                    _extract(df, [sym])
                    _time.sleep(1.0)
                except Exception:
                    continue

        if out:
            logger.warning(
                f"_safe_yf_download: got {len(out)}/{len(tickers)} tickers "
                f"({len(out)/len(tickers)*100:.0f}% coverage)"
            )
    except Exception as e:
        logger.warning(f"_safe_yf_download soft-fail: {e}")
    return out


def _compute_simple_signal(closes, lookback: int = 20) -> float:
    """Lightweight momentum + trend signal. Same direction as the live
    composite_score but much cheaper to compute on every day of every
    ticker.

    Returns a float in roughly [-3, +3] range:
      positive = bullish, negative = bearish, magnitude = strength
    """
    try:
        import numpy as np
        if len(closes) < lookback + 5:
            return 0.0
        recent = closes[-lookback:]
        cur = float(recent[-1])
        # Momentum: % return over lookback
        ret = (cur / float(recent[0]) - 1) * 100
        # Trend strength: % above SMA20
        sma = float(np.mean(recent))
        sma_dist = (cur / sma - 1) * 100
        # Recent acceleration (last 5d vs prior 15d)
        last5 = float(np.mean(recent[-5:]))
        prior = float(np.mean(recent[:-5]))
        accel = (last5 / prior - 1) * 100 if prior > 0 else 0
        # Combined score
        score = (ret * 0.4) + (sma_dist * 0.4) + (accel * 0.2)
        # Clamp to plausible range
        return max(-5.0, min(5.0, score / 2.0))
    except Exception:
        return 0.0


def run_backtest(start_date: str = None,
                 end_date: str = None,
                 tickers: list = None,
                 top_n: int = DEFAULT_TOP_N,
                 hold_days: int = DEFAULT_HOLD_DAYS,
                 stop_pct: float = DEFAULT_STOP_PCT,
                 take_pct: float = DEFAULT_TAKE_PCT,
                 initial_capital: float = DEFAULT_INITIAL_CAPITAL,
                 position_pct: float = DEFAULT_POSITION_PCT,
                 include_internals: bool = False) -> dict:
    """Replay a momentum-based long-only strategy over historical data.

    Args:
        start_date: 'YYYY-MM-DD' or None (defaults to 1 year ago)
        end_date: 'YYYY-MM-DD' or None (defaults to today)
        tickers: list of symbols, or None (uses a default 30-stock subset)
        top_n: how many positions to hold simultaneously
        hold_days: avg holding period
        stop_pct: stop loss % (e.g., 0.04 = 4%)
        take_pct: take profit % (e.g., 0.10 = 10%)
        initial_capital: starting cash
        position_pct: % of cash per position

    Returns metrics dict. Never raises.
    """
    try:
        import pandas as pd
        import numpy as np

        if not end_date:
            end_date = datetime.utcnow().strftime("%Y-%m-%d")
        if not start_date:
            start_date = (datetime.utcnow() - timedelta(days=365)).strftime("%Y-%m-%d")

        # Default universe — 100 high-liquidity US stocks across all sectors
        # Broader universe = better learning from historical patterns, not just
        # the 30 names we've previously traded. This is what makes the
        # auto-fixer's insights statistically meaningful.
        if not tickers:
            tickers = [
                # Mega-cap tech
                "AAPL","MSFT","GOOGL","AMZN","META","NVDA","TSLA","AVGO","ORCL","CRM",
                "AMD","NFLX","ADBE","INTC","QCOM","CSCO","IBM","TXN","PYPL","SHOP",
                # Financials
                "JPM","BAC","GS","MS","WFC","C","V","MA","BLK","SCHW",
                "AXP","COF","USB","PNC","TFC","BX","KKR","SPGI","ICE","CME",
                # Healthcare
                "JNJ","UNH","PFE","LLY","ABBV","TMO","DHR","BMY","ABT","MRK",
                "AMGN","CVS","ELV","ISRG","GILD","REGN","VRTX","HUM",
                # Energy
                "XOM","CVX","COP","SLB","EOG","PSX","MPC","OXY","HAL","VLO",
                # Consumer
                "HD","WMT","COST","KO","PEP","DIS","NKE","MCD","SBUX","TGT",
                "LOW","TJX","BKNG","CMG","DG","ROST","YUM","ABNB",
                # Industrials
                "BA","CAT","GE","HON","UNP","UPS","RTX","DE","LMT","NOC",
                # Communication / Media
                "T","VZ","TMUS","CMCSA","CHTR",
                # Utilities + Materials
                "NEE","SO","DUK","LIN","APD","FCX",
            ]

        # Download all historical data
        prices = _safe_yf_download(tickers, start_date, end_date)
        if not prices:
            return {"ok": False, "reason": "no_price_data_returned"}

        # SP500 benchmark
        sp = _safe_yf_download(["SPY"], start_date, end_date)
        sp_series = sp.get("SPY")

        # Build aligned date index (intersection of all tickers + SPY)
        all_dates = None
        for sym, s in prices.items():
            dates_set = set(s.index)
            all_dates = dates_set if all_dates is None else (all_dates & dates_set)
        if not all_dates:
            return {"ok": False, "reason": "no_common_trading_dates"}
        date_list = sorted(all_dates)
        if len(date_list) < 60:
            return {"ok": False, "reason": f"too_few_dates ({len(date_list)})"}

        # Walk forward day by day
        cash = float(initial_capital)
        positions = {}  # ticker -> {entry_price, shares, entry_date, entry_idx}
        trades = []   # list of {ticker, entry_price, exit_price, pnl_pct, days_held, exit_reason}
        equity_curve = []  # list of (date, total_equity)

        for i, d in enumerate(date_list):
            # Need at least 25 days of history for signal
            if i < 25:
                equity_curve.append((d.strftime("%Y-%m-%d"), cash))
                continue

            # Mark-to-market: total equity = cash + sum(open positions)
            position_value = 0.0
            for sym, p in list(positions.items()):
                cur_price = float(prices[sym].iloc[i])
                position_value += p["shares"] * cur_price

            total_equity = cash + position_value
            equity_curve.append((d.strftime("%Y-%m-%d"), total_equity))

            # Exit checks
            for sym, p in list(positions.items()):
                cur_price = float(prices[sym].iloc[i])
                pnl_pct = (cur_price / p["entry_price"] - 1)
                days_held = (d - p["entry_date"]).days
                exit_reason = None

                if pnl_pct <= -stop_pct:
                    exit_reason = "stop_loss"
                elif pnl_pct >= take_pct:
                    exit_reason = "take_profit"
                elif days_held >= hold_days:
                    exit_reason = "time_stop"

                if exit_reason:
                    cash += p["shares"] * cur_price
                    trades.append({
                        "ticker": sym,
                        "entry_date": p["entry_date"].strftime("%Y-%m-%d") if hasattr(p["entry_date"], "strftime") else str(p["entry_date"])[:10],
                        "exit_date": d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10],
                        "entry_price": round(p["entry_price"], 2),
                        "exit_price": round(cur_price, 2),
                        "pnl_pct": round(pnl_pct * 100, 2),
                        "days_held": days_held,
                        "exit_reason": exit_reason,
                    })
                    del positions[sym]

            # Compute signals for all tickers
            signals = []
            for sym, s in prices.items():
                if sym in positions:
                    continue  # already holding
                window = s.iloc[max(0, i-25):i+1].values.astype(float)
                if len(window) < 25:
                    continue
                score = _compute_simple_signal(window)
                signals.append((sym, score))

            # Pick top N longs
            signals.sort(key=lambda x: x[1], reverse=True)
            slots_open = top_n - len(positions)
            for sym, score in signals[:slots_open]:
                if score < 0.5:
                    continue  # skip weak signals
                price = float(prices[sym].iloc[i])
                position_dollars = total_equity * position_pct
                if position_dollars > cash:
                    continue
                shares = position_dollars / price
                positions[sym] = {
                    "entry_price": price,
                    "shares": shares,
                    "entry_date": d,
                    "entry_idx": i,
                }
                cash -= shares * price

        # Final equity = cash + remaining positions at last close
        final_position_value = 0.0
        last_idx = len(date_list) - 1
        for sym, p in positions.items():
            final_position_value += p["shares"] * float(prices[sym].iloc[last_idx])
        final_equity = cash + final_position_value

        # Metrics
        total_return = (final_equity / initial_capital - 1) * 100

        # Sharpe (annualized)
        equity_series = [e[1] for e in equity_curve]
        daily_rets = []
        for j in range(1, len(equity_series)):
            if equity_series[j-1] > 0:
                daily_rets.append(equity_series[j] / equity_series[j-1] - 1)
        if daily_rets:
            mean_r = sum(daily_rets) / len(daily_rets)
            std_r = (sum((r - mean_r)**2 for r in daily_rets) / len(daily_rets)) ** 0.5
            sharpe = (mean_r / std_r * (252**0.5)) if std_r > 0 else 0
        else:
            sharpe = 0

        # Max drawdown
        peak = equity_series[0] if equity_series else initial_capital
        max_dd = 0
        for v in equity_series:
            if v > peak:
                peak = v
            dd = (v - peak) / peak * 100 if peak > 0 else 0
            if dd < max_dd:
                max_dd = dd

        # Win rate + profit factor on closed trades only
        closed_trades = [t for t in trades]
        if closed_trades:
            wins = [t for t in closed_trades if t["pnl_pct"] > 0]
            losses = [t for t in closed_trades if t["pnl_pct"] <= 0]
            win_rate = len(wins) / len(closed_trades) * 100
            gross_win = sum(t["pnl_pct"] for t in wins)
            gross_loss = abs(sum(t["pnl_pct"] for t in losses))
            profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (gross_win if gross_win > 0 else 0)
            avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
            avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
            best = max(closed_trades, key=lambda t: t["pnl_pct"])
            worst = min(closed_trades, key=lambda t: t["pnl_pct"])
        else:
            win_rate = 0; profit_factor = 0; avg_win = 0; avg_loss = 0
            best = worst = None

        # Per-ticker breakdown (for auto-fixer insight extraction)
        per_ticker = {}
        try:
            from collections import defaultdict
            tk = defaultdict(lambda: {"trades": 0, "wins": 0, "total_pnl_pct": 0.0})
            for t in closed_trades:
                sym = t.get("ticker")
                if not sym:
                    continue
                tk[sym]["trades"] += 1
                if t["pnl_pct"] > 0:
                    tk[sym]["wins"] += 1
                tk[sym]["total_pnl_pct"] += float(t["pnl_pct"])
            for sym, d in tk.items():
                n = d["trades"]
                per_ticker[sym] = {
                    "trades": n,
                    "wins": d["wins"],
                    "win_rate_pct": round(d["wins"] / n * 100, 2) if n else 0,
                    "avg_pnl_pct": round(d["total_pnl_pct"] / n, 3) if n else 0,
                    "total_pnl_pct": round(d["total_pnl_pct"], 3),
                }
        except Exception as _e:
            logger.debug(f"per_ticker breakdown soft-fail: {_e}")

        # SP500 buy-and-hold return
        sp_return = None
        if sp_series is not None and len(sp_series) >= 2:
            sp_return = (float(sp_series.iloc[-1]) / float(sp_series.iloc[0]) - 1) * 100

        result = {
            "ok": True,
            "config": {
                "start_date": start_date,
                "end_date": end_date,
                "tickers_count": len(prices),
                "trading_days": len(date_list),
                "top_n_positions": top_n,
                "hold_days": hold_days,
                "stop_pct": stop_pct * 100,
                "take_pct": take_pct * 100,
                "initial_capital": initial_capital,
                "position_pct": position_pct * 100,
            },
            "results": {
                "final_equity": round(final_equity, 2),
                "total_return_pct": round(total_return, 2),
                "sp500_return_pct": round(sp_return, 2) if sp_return is not None else None,
                "alpha_vs_sp500_pct": round(total_return - sp_return, 2) if sp_return is not None else None,
                "sharpe_ratio": round(sharpe, 2),
                "max_drawdown_pct": round(max_dd, 2),
                "total_trades": len(closed_trades),
                "win_rate_pct": round(win_rate, 2),
                "profit_factor": round(profit_factor, 2),
                "avg_win_pct": round(avg_win, 2),
                "avg_loss_pct": round(avg_loss, 2),
                "best_trade": best,
                "worst_trade": worst,
                "per_ticker": per_ticker,
            },
            "computed_at": datetime.utcnow().isoformat(),
        }

        # Optionally include internals (equity curve + full trade list +
        # SPY series) for downstream analyzers like backtest_pro.py.
        # Skipped by default to keep API responses light.
        if include_internals:
            try:
                result["_internals"] = {
                    "equity_curve": [
                        {"date": d, "equity": float(e)} for d, e in equity_curve
                    ],
                    "trades": closed_trades,  # already serializable dicts
                    "sp500_series": (
                        [{"date": idx.strftime("%Y-%m-%d"), "close": float(v)}
                         for idx, v in sp_series.items()]
                        if sp_series is not None else []
                    ),
                }
            except Exception as _ie:
                logger.debug(f"include_internals serialization soft-fail: {_ie}")
                result["_internals"] = {"equity_curve": [], "trades": [],
                                        "sp500_series": []}

        # Cache
        try:
            _last_backtest["data"] = result
            _last_backtest["ts"] = time.time()
        except Exception:
            pass

        return result

    except Exception as e:
        logger.warning(f"run_backtest soft-fail: {e}")
        return {"ok": False, "reason": str(e)[:300]}


def get_backtest_summary() -> dict:
    """Return cached backtest result, if fresh. Else returns placeholder."""
    try:
        if _last_backtest.get("data"):
            age = time.time() - _last_backtest.get("ts", 0)
            out = dict(_last_backtest["data"])
            out["_cache_age_seconds"] = round(age, 1)
            out["_cache_fresh"] = age < _BACKTEST_TTL
            return out
        return {"ok": False, "reason": "no_backtest_run_yet",
                "message": "Run /api/backtest?days=365 first"}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}
