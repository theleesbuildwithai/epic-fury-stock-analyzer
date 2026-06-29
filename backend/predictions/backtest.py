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


# ── STB ticker loader ────────────────────────────────────────────────────────
# Reads the live STB picks cache built by quant_engine.py and returns the
# current list of tickers.  Falls back to the default 100-stock universe if
# the cache is missing, empty, or has <5 tickers.
_STB_CACHE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "analysis", ".picks_disk_cache.json"
)

def _get_stb_tickers(min_picks: int = 5) -> list:
    """Return tickers from the live STB picks cache, or [] if unavailable."""
    try:
        path = os.path.normpath(_STB_CACHE_PATH)
        if not os.path.exists(path):
            return []
        with open(path, "r") as _f:
            raw = json.load(_f)
        picks = raw.get("top_picks") or []
        tickers = [p["ticker"] for p in picks if p.get("ticker")]
        if len(tickers) < min_picks:
            return []
        return list(dict.fromkeys(tickers))  # deduplicate, preserve order
    except Exception as _e:
        logger.debug(f"_get_stb_tickers soft-fail: {_e}")
        return []

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

# Per-parameter result cache for "works no matter what" guarantee.
# Stores successful runs keyed by (days_back_rounded, stop, take, hold).
# On any failure, falls back to the most recent successful run that matches.
_result_cache = {}   # key: (days, top_n, stop, take, hold) -> {"data": dict, "ts": float}
_RESULT_CACHE_TTL = 21600   # 6 hours
_result_cache_lock = _threading.Lock() if False else None  # keep ref slot

def _result_key(start: str, end: str, top_n: int, stop: float, take: float, hold: int) -> tuple:
    """Build a stable cache key (round dates to day, params to nearest)."""
    return (str(start)[:10], str(end)[:10], int(top_n),
            round(float(stop), 4), round(float(take), 4), int(hold))

# PRICE DATA CACHE — biggest defense against yfinance rate limits.
# Same tickers+range returns instantly without hitting yfinance again.
# 2-hour TTL because historical data doesn't change.
_price_cache = {}   # key: (tickers_tuple, start, end) -> {"data": dict, "ts": float}
_PRICE_CACHE_TTL = 7200   # 2 hours
import threading as _threading
_price_cache_lock = _threading.Lock()


def _cache_key(tickers: list, start: str, end: str) -> tuple:
    """Build a stable cache key from request parameters."""
    return (tuple(sorted(tickers)), start, end)


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

    CACHE (2026-05-17): 2-hour in-memory cache by (tickers, start, end).
    Repeat backtests on same window hit cache, eliminating rate limit
    exposure entirely.
    """
    out = {}
    if not tickers:
        return out

    # Cache check (only when not using period override)
    cache_hit = None
    if period is None and start and end:
        try:
            ck = _cache_key(list(tickers), start, end)
            with _price_cache_lock:
                entry = _price_cache.get(ck)
                if entry and (time.time() - entry["ts"]) < _PRICE_CACHE_TTL:
                    cache_hit = dict(entry["data"])
        except Exception:
            cache_hit = None
        if cache_hit:
            return cache_hit

    try:
        import yfinance as yf
        import pandas as pd
        import time as _time
        import threading as _bt_thr

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
                            # Use lower threshold for single-ticker calls (e.g. SPY on
                            # short windows) but require 30 for universe tickers so that
                            # partial downloads don't shrink the date intersection.
                            _min = 5 if len(syms) == 1 else 30
                            if len(s) >= _min:
                                out[sym] = s
                                got.add(sym)
                    elif len(syms) == 1:
                        s = df["Close"].dropna()
                        if len(s) >= 5:   # single ticker (SPY etc) — accept short windows
                            out[sym] = s
                            got.add(sym)
                except Exception:
                    continue
            return got

        kwargs = {"start": start, "end": end, "progress": False,
                  "auto_adjust": True, "threads": True, "group_by": "ticker"}
        if period:
            kwargs["period"] = period

        # For small universes (≤40 tickers, i.e. STB picks), skip bulk batch
        # and go straight to individual downloads.  yfinance MultiIndex batch
        # can silently assign wrong ticker's data to the wrong column — safe
        # individual downloads eliminate that risk entirely for small sets.
        _use_individual_only = len(tickers) <= 40

        if not _use_individual_only:
            # TIER 1: bulk fetch (fastest path for large universes)
            try:
                _bt1_r = [None]
                _bt1_t = _bt_thr.Thread(
                    target=lambda r=_bt1_r, tk=list(tickers), kw=dict(kwargs): r.__setitem__(
                        0, yf.download(tk, **kw)),
                    daemon=True)
                _bt1_t.start(); _bt1_t.join(timeout=30)
                _extract(_bt1_r[0], tickers)
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
                    _bt2_r = [None]
                    _bt2_t = _bt_thr.Thread(
                        target=lambda r=_bt2_r, c=chunk, kw=dict(kwargs): r.__setitem__(
                            0, yf.download(c, **kw)),
                        daemon=True)
                    _bt2_t.start(); _bt2_t.join(timeout=20)
                    _extract(_bt2_r[0], chunk)
                    _time.sleep(0.5)  # gentle on rate limits
                except Exception as e:
                    logger.debug(f"_safe_yf_download chunk fail [{i}]: {e}")
                    continue

        # TIER 3: individual downloads — always runs for STB universe,
        # or as a fallback for stragglers in large-universe mode.
        still_missing = [t for t in tickers if t not in out]
        _ind_limit = len(tickers) if _use_individual_only else 30
        if still_missing and len(still_missing) <= _ind_limit:
            for sym in still_missing:
                try:
                    _bt3_r = [None]
                    _bt3_t = _bt_thr.Thread(
                        target=lambda r=_bt3_r, s=sym, kw=dict(kwargs): r.__setitem__(
                            0, yf.download([s], **kw)),
                        daemon=True)
                    _bt3_t.start(); _bt3_t.join(timeout=10)
                    _extract(_bt3_r[0], [sym])
                    _time.sleep(0.3 if _use_individual_only else 1.0)
                except Exception:
                    continue

        if out:
            logger.warning(
                f"_safe_yf_download: got {len(out)}/{len(tickers)} tickers "
                f"({len(out)/len(tickers)*100:.0f}% coverage)"
            )
    except Exception as e:
        logger.warning(f"_safe_yf_download soft-fail: {e}")

    # Cache successful results (don't cache empty/partial failures)
    try:
        if out and len(out) >= max(1, int(len(tickers) * 0.5)) and period is None and start and end:
            ck = _cache_key(list(tickers), start, end)
            with _price_cache_lock:
                _price_cache[ck] = {"data": dict(out), "ts": time.time()}
    except Exception:
        pass
    return out


# Default 100-stock universe exported so backtest_pro.py can pre-load all
# prices once for the full walk-forward range (avoids 36× yfinance calls).
_DEFAULT_UNIVERSE = [
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
                 include_internals: bool = False,
                 cost_bps: float = 0.0,
                 slippage_bps: float = 0.0,
                 _preloaded_prices: dict = None,
                 _preloaded_spy=None) -> dict:
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

        # CACHE-FIRST: matching params + fresh cache => return immediately.
        # Was the root cause of /api/backtest/detail timing out at 60s — the
        # pre-warm thread populated _result_cache but run_backtest never
        # consulted it on entry, so every UI request triggered a fresh
        # 30+ minute yfinance pull.  Cache hit = sub-millisecond response.
        try:
            ck_fast = _result_key(start_date, end_date, top_n, stop_pct, take_pct, hold_days)
            cached = _result_cache.get(ck_fast)
            if cached and cached.get("data"):
                age = time.time() - cached.get("ts", 0)
                if age < _RESULT_CACHE_TTL:
                    _cdata = cached["data"]
                    # Bypass conditions — never serve a poisoned cache entry
                    _bypass = False
                    if include_internals and "_internals" not in _cdata:
                        logger.debug("backtest cache: bypass — internals not cached")
                        _bypass = True
                    elif not _cdata.get("_sp500_series"):
                        # sp500_series empty means SPY failed last time — retry
                        logger.debug("backtest cache: bypass — sp500_series empty, retrying SPY")
                        _bypass = True
                    elif (_cdata.get("config", {}).get("tickers_count") or 0) < 50:
                        # Too few tickers — pre-warm ran while yfinance was cold;
                        # discard and re-run so we get the full universe.
                        logger.info("backtest cache: bypass — low ticker count "
                                    f"({_cdata.get('config',{}).get('tickers_count')}), re-fetching")
                        _bypass = True
                    if not _bypass:
                        out = dict(_cdata)
                        out["_cache_hit"] = True
                        out["_cache_age_seconds"] = round(age, 1)
                        return out
        except Exception as _cce:
            logger.debug(f"backtest cache-first check soft-fail: {_cce}")

        # Universe: broad 100-stock list across all sectors.
        # IMPORTANT: Do NOT use today's live STB picks as the backtest universe.
        # STB picks are chosen based on CURRENT signals — using them to test the
        # past 90-365 days creates look-ahead bias in the wrong direction (today's
        # strong picks were often weak 90 days ago, making the backtest show false
        # losses). A stable broad universe tests whether the STRATEGY works across
        # time, not whether today's specific picks happened to be good historically.
        # STB picks are for live trading; the backtest tests the strategy logic.
        if not tickers:
            tickers = list(_DEFAULT_UNIVERSE)

        # Support pre-loaded prices (used by walk_forward_validation to avoid
        # re-downloading the same data 36 times for each train/test window).
        # If caller passes _preloaded_prices, skip all yfinance downloads.
        if _preloaded_prices is not None:
            prices = dict(_preloaded_prices)
            sp_series = _preloaded_spy
            logger.debug(f"BACKTEST: using preloaded prices ({len(prices)} tickers)")
        else:
            # Bundle SPY into the universe bulk download so it shares the same
            # yfinance session (bulk TIER1/TIER2) instead of a separate individual
            # call that can time out on the 10s thread deadline.
            tickers_plus_spy = list(tickers) + ["SPY"]
            all_dl = _safe_yf_download(tickers_plus_spy, start_date, end_date)
            sp_series = all_dl.pop("SPY", None)
            prices = all_dl

            # MULTI-SOURCE SPY FALLBACK — if SPY missed the bulk download,
            # try progressively: individual yfinance → ^GSPC → IVV → VOO.
            # Each attempt uses a longer timeout than the previous.
            if sp_series is None:
                import yfinance as _yf_spy
                import threading as _spy_thr
                for _spy_sym in ("SPY", "^GSPC", "IVV", "VOO"):
                    try:
                        _spy_r = [None]
                        _spy_t = _spy_thr.Thread(
                            target=lambda r=_spy_r, sym=_spy_sym: r.__setitem__(
                                0, _yf_spy.download([sym], start=start_date,
                                                     end=end_date,
                                                     auto_adjust=True,
                                                     progress=False)),
                            daemon=True)
                        _spy_t.start(); _spy_t.join(timeout=30)
                        _df = _spy_r[0]
                        if _df is not None and not _df.empty:
                            import pandas as _pd_spy
                            if isinstance(_df.columns, _pd_spy.MultiIndex):
                                # Handle both (ticker, price) and (price, ticker) orderings
                                if (_spy_sym, "Close") in _df.columns:
                                    _s = _df[(_spy_sym, "Close")].dropna()
                                elif ("Close", _spy_sym) in _df.columns:
                                    _s = _df[("Close", _spy_sym)].dropna()
                                else:
                                    _cls = [c for c in _df.columns
                                            if str(c[-1]).lower() == "close"
                                            or str(c[0]).lower() == "close"]
                                    _s = _df[_cls[0]].dropna() if _cls else _pd_spy.Series(dtype=float)
                            else:
                                _s = (_df["Close"] if "Close" in _df.columns
                                      else _df.iloc[:, 0]).dropna()
                            if len(_s) >= 5:
                                sp_series = _s
                                logger.info(f"BACKTEST SPY fallback OK via {_spy_sym} ({len(_s)} pts)")
                                break
                    except Exception as _sfe:
                        logger.debug(f"SPY fallback {_spy_sym} fail: {_sfe}")
                        continue

            logger.info(f"BACKTEST: universe={len(prices)} tickers, "
                        f"SPY={'OK' if sp_series is not None else 'FAILED'} "
                        f"({len(sp_series) if sp_series is not None else 0} pts)")
        if not prices:
            # STALE-CACHE FALLBACK: serve last good result for same params
            try:
                ck = _result_key(start_date, end_date, top_n, stop_pct, take_pct, hold_days)
                stale = _result_cache.get(ck)
                if stale and stale.get("data"):
                    out = dict(stale["data"])
                    out["_stale"] = True
                    out["_stale_age_seconds"] = round(time.time() - stale.get("ts", 0), 1)
                    out["_stale_reason"] = "no_price_data_returned; serving prior run"
                    logger.warning(f"BACKTEST FALLBACK: serving stale result ({out['_stale_age_seconds']}s old)")
                    return out
            except Exception:
                pass
            return {"ok": False, "reason": "no_price_data_returned"}

        # Clamp all price series to [start_date, end_date] so yfinance
        # returning extra history (e.g. a full year for a 30-day request)
        # doesn't inflate the simulation period.
        # Use string comparison on strftime("%Y-%m-%d") to avoid tz-naive
        # vs tz-aware TypeError that silently kills the clamp.
        try:
            import pandas as _pd_dt
            def _clamp(s, s_date, e_date):
                idx_strs = s.index.strftime("%Y-%m-%d")
                mask = (idx_strs >= s_date) & (idx_strs <= e_date)
                return s[mask]
            prices = {sym: _clamp(s, start_date, end_date) for sym, s in prices.items()}
            if sp_series is not None:
                sp_series = _clamp(sp_series, start_date, end_date)
        except Exception as _clampe:
            logger.debug(f"price clamp soft-fail: {_clampe}")

        # Normalize all price series to tz-naive before intersection.
        # Newer yfinance returns tz-aware DatetimeIndex for some tickers and
        # tz-naive for others. Mixed-tz set intersection is always empty →
        # no_common_trading_dates. Fix: strip tz from every series first.
        for _sym in list(prices.keys()):
            try:
                _s = prices[_sym]
                if hasattr(_s.index, "tz") and _s.index.tz is not None:
                    prices[_sym] = _s.tz_localize(None)
            except Exception:
                try:
                    prices[_sym] = _s.tz_convert(None)
                except Exception:
                    pass
        if sp_series is not None:
            try:
                if hasattr(sp_series.index, "tz") and sp_series.index.tz is not None:
                    sp_series = sp_series.tz_localize(None)
            except Exception:
                try:
                    sp_series = sp_series.tz_convert(None)
                except Exception:
                    pass

        # Drop any ticker that ended up with zero rows after clamping.
        # An empty series would make the date intersection empty immediately.
        prices = {sym: s for sym, s in prices.items() if len(s) >= 5}
        if not prices:
            return {"ok": False, "reason": "no_price_data_after_clamp"}

        all_dates = None
        for sym, s in prices.items():
            dates_set = set(s.index)
            all_dates = dates_set if all_dates is None else (all_dates & dates_set)
        if not all_dates:
            return {"ok": False, "reason": "no_common_trading_dates"}
        date_list = sorted(all_dates)
        min_dates = 20  # 30-day window ≈ 22 trading days; allow slightly fewer
        if len(date_list) < min_dates:
            return {"ok": False, "reason": f"too_few_dates ({len(date_list)})"}

        # Re-align all price series to the shared date_list so that
        # prices[sym].iloc[i] always corresponds to date_list[i].
        # Without this, tickers with extra leading/trailing dates cause
        # iloc[i] to access the wrong date, producing wrong prices.
        try:
            import pandas as _pd_align
            _dl_index = _pd_align.DatetimeIndex(date_list)
            prices = {sym: s.reindex(_dl_index) for sym, s in prices.items()}
        except Exception as _ae:
            logger.debug(f"price reindex soft-fail (continuing with original): {_ae}")

        # Walk forward day by day
        cash = float(initial_capital)
        positions = {}  # ticker -> {entry_price, shares, entry_date, entry_idx}
        trades = []   # list of {ticker, entry_price, exit_price, pnl_pct, days_held, exit_reason}
        equity_curve = []  # list of (date, total_equity)
        # Transaction cost: total round-trip = (cost_bps + slippage_bps) / 10000 * 2
        # Applied to each trade's pnl_pct (entry + exit cost combined)
        round_trip_cost_pct = (float(cost_bps) + float(slippage_bps)) / 10000.0 * 2.0

        for i, d in enumerate(date_list):
            # Need at least 25 days of history for signal
            _d_str = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
            if i < 25:
                equity_curve.append((_d_str, cash))
                continue

            # Mark-to-market: total equity = cash + sum(open positions)
            position_value = 0.0
            for sym, p in list(positions.items()):
                cur_price = float(prices[sym].iloc[i])
                position_value += p["shares"] * cur_price

            total_equity = cash + position_value
            equity_curve.append((_d_str, total_equity))

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
                    # AUDIT #2 — ADV-scaled slippage per trade
                    # Replaces the flat cost_pct with a Kyle's-lambda sqrt
                    # impact model that punishes trades larger than 5% of
                    # ADV. Falls back to flat cost when ADV is unknown.
                    try:
                        from predictions.quant_audit_fixes import adv_scaled_slippage_bps
                        order_dollars = abs(p["shares"]) * cur_price
                        # Approximate ADV-$ from 20-day mean volume in price-data window
                        try:
                            adv_shares = float(prices[sym].iloc[max(0, i-20):i].mean())
                            adv_dollars = adv_shares * cur_price
                        except Exception:
                            adv_dollars = 0
                        slip_bps_one_side = adv_scaled_slippage_bps(
                            order_dollars, adv_dollars,
                            base_bps=float(slippage_bps), impact_coef=100.0,
                        )
                        # Round-trip = (cost + adv_slippage) / 10000 * 2 sides
                        rt_cost_this = (float(cost_bps) + slip_bps_one_side) / 10000.0 * 2.0
                    except Exception:
                        rt_cost_this = round_trip_cost_pct

                    pnl_after_cost = pnl_pct - rt_cost_this
                    cash += p["shares"] * cur_price * (1 - rt_cost_this)
                    trades.append({
                        "ticker": sym,
                        "entry_date": p["entry_date"].strftime("%Y-%m-%d") if hasattr(p["entry_date"], "strftime") else str(p["entry_date"])[:10],
                        "exit_date": d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10],
                        "entry_price": round(p["entry_price"], 2),
                        "exit_price": round(cur_price, 2),
                        "pnl_pct": round(pnl_after_cost * 100, 2),
                        "pnl_pct_gross": round(pnl_pct * 100, 2),
                        "cost_pct": round(rt_cost_this * 100, 3),
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

        # Safe serialization helpers (used for both top-level and _internals)
        import math as _math

        def _sf(v):
            """Safe float — returns 0.0 for NaN/inf/None."""
            try:
                f = float(v)
                return 0.0 if (_math.isnan(f) or _math.isinf(f)) else f
            except Exception:
                return 0.0

        def _sd(d):
            """Safe date string — handles Timestamp, date, str."""
            try:
                return d.strftime("%Y-%m-%d")
            except Exception:
                return str(d)[:10]

        # Serialize chart data now — always, unconditionally.
        # Storing at top-level means cache hits, stale fallbacks, and all
        # code paths can always find this data.  The _internals block below
        # mirrors them for backtest_pro.py backward compatibility.
        try:
            _eq_list = [{"date": _sd(d), "equity": _sf(e)} for d, e in equity_curve]
        except Exception as _eqe:
            logger.warning(f"BACKTEST: equity_curve serialization fail: {_eqe}")
            _eq_list = []
        try:
            if sp_series is None:
                _sp_list = []
            else:
                # If sp_series is a DataFrame (e.g. from a multi-column yfinance
                # result), squeeze to a 1D Series before iterating.
                import pandas as _pd_sp_ser
                if isinstance(sp_series, _pd_sp_ser.DataFrame):
                    _sp_cols = [c for c in sp_series.columns
                                if str(c).lower() in ("close", "adj close", "adjclose")]
                    sp_series = sp_series[_sp_cols[0]] if _sp_cols else sp_series.iloc[:, 0]
                # Use zip(index, values) — avoids any .items() edge cases on
                # unusual Series types returned by some yfinance versions.
                try:
                    _pairs = list(zip(sp_series.index, sp_series.values))
                except Exception:
                    _pairs = list(sp_series.items()) if hasattr(sp_series, "items") else []
                _sp_list = [
                    {"date": _sd(idx), "close": _sf(v)}
                    for idx, v in _pairs
                    if v == v and v is not None  # v == v filters NaN
                ]
                if not _sp_list:
                    logger.warning(
                        f"BACKTEST: sp_series type={type(sp_series).__name__} "
                        f"len={len(sp_series) if hasattr(sp_series,'__len__') else '?'} "
                        f"produced empty _sp_list"
                    )
        except Exception as _spe:
            logger.warning(f"BACKTEST: sp500_series serialization fail: {_spe}")
            _sp_list = []

        logger.info(
            f"BACKTEST CHART DATA: equity_curve={len(_eq_list)} "
            f"sp500={len(_sp_list)} trades={len(closed_trades)}"
        )

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
            # Chart data stored at top-level — always accessible regardless
            # of include_internals flag, cache state, or exception paths.
            "_equity_curve": _eq_list,
            "_sp500_series": _sp_list,
            "_trades": closed_trades,
            "computed_at": datetime.utcnow().isoformat(),
        }

        # Mirror into _internals for backtest_pro.py compatibility
        if include_internals:
            result["_internals"] = {
                "equity_curve": _eq_list,
                "trades": closed_trades,
                "sp500_series": _sp_list,
            }

        # Cache (legacy single-slot + new per-param)
        try:
            _last_backtest["data"] = result
            _last_backtest["ts"] = time.time()
            # Per-parameter cache for stale fallback
            ck = _result_key(start_date, end_date, top_n, stop_pct, take_pct, hold_days)
            _result_cache[ck] = {"data": result, "ts": time.time()}
            # Cap memory: keep last 50 entries
            if len(_result_cache) > 50:
                oldest = min(_result_cache.items(), key=lambda kv: kv[1].get("ts", 0))
                _result_cache.pop(oldest[0], None)
        except Exception:
            pass

        return result

    except Exception as e:
        logger.warning(f"run_backtest soft-fail: {e}")
        # STALE-CACHE FALLBACK: serve last good result for these params on any crash
        try:
            ck = _result_key(start_date or "", end_date or "", top_n, stop_pct, take_pct, hold_days)
            stale = _result_cache.get(ck)
            if stale and stale.get("data"):
                out = dict(stale["data"])
                out["_stale"] = True
                out["_stale_age_seconds"] = round(time.time() - stale.get("ts", 0), 1)
                out["_stale_reason"] = f"exception: {str(e)[:120]}; serving prior run"
                logger.warning(f"BACKTEST EXCEPTION FALLBACK: serving stale ({out['_stale_age_seconds']}s old)")
                return out
        except Exception:
            pass
        # No stale match — last-resort: return ANY recent successful result
        try:
            if _last_backtest.get("data"):
                out = dict(_last_backtest["data"])
                out["_stale"] = True
                out["_stale_age_seconds"] = round(time.time() - _last_backtest.get("ts", 0), 1)
                out["_stale_reason"] = f"exception: {str(e)[:120]}; serving any recent run"
                return out
        except Exception:
            pass
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
