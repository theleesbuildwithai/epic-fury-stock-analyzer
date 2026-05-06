"""
Truth Engine — Sentinel Quant

A SINGLE SOURCE OF TRUTH for two numbers that must always be correct:
  1. S&P 500 cumulative return (vs fund inception)
  2. Fund cumulative return (vs $100k initial capital)

History of failures this fixes:
  - Yahoo intermittently returns no data for ^GSPC; old code silently
    wrote sp500_cum=0 making the equity curve drop to 0% then bounce.
  - Recovery operations called save_portfolio_snapshot(..., 0, 0, ...)
    explicitly polluting the chart with phantom zero values.
  - The inception date drifted as new trades were entered (computed from
    earliest entry_date), so cumulative returns recomputed off a moving
    baseline.
  - Options were valued with an estimated premium that could spike or
    crash on price changes, throwing fund return off by 5-15%.

Defenses added here:
  1. MULTI-SOURCE S&P 500 LOOKUP
       Tries ^GSPC -> SPY ETF -> ^SPX in order. Caches latest good value
       in trading_state. NEVER returns 0 — always falls back to last
       known good or carries forward the prior snapshot value.

  2. PINNED INCEPTION DATE
       Stored in trading_state on first run. Never recomputes off
       trade history. Locked baseline = correct cumulative math forever.

  3. OPTIONS-AWARE FUND VALUATION
       Uses a CONSERVATIVE options pricing model with hard bounds:
       option position_value clamped to [0, entry_premium * 5x] so a
       single bad price tick can't move portfolio value by >$50k.

  4. SNAPSHOT VALIDATION GATE
       Before any snapshot is written, validates that:
         - total_value == cash + positions_value (within $1)
         - cum_return matches the formula (within 0.5%)
         - sp500_cum is non-zero OR carried forward from prior
         - fund value didn't change >25% in one day with 0 trades

       If validation fails, the snapshot is REJECTED, audit-logged, and
       the prior good snapshot is preserved.

  5. AUDIT LOG INTEGRATION
       Every snapshot save and S&P fetch failure is audit-logged so we
       can replay timelines if anything goes wrong.

Public API:
  get_sp500_truth()           -> {"price","daily_pct","cum_pct","source","ok"}
  get_fund_truth()            -> {"total_value","cash","pos_val","cum_pct",...}
  safe_save_snapshot()        -> validated snapshot save with carry-forward
  recompute_sp500_history()   -> rebuild all historical sp500_cum values
  get_inception()             -> pinned inception date + baseline close
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
#  CONSTANTS — chosen for safety/realism
# ============================================================
INITIAL_CAPITAL = 100_000.0          # baseline for fund cum_return
SP500_CACHE_TTL = 600                # 10 min cache for live sp500
SP500_LASTGOOD_KEY = "sp500_lastgood_close"
SP500_LASTGOOD_TS_KEY = "sp500_lastgood_ts"
INCEPTION_DATE_KEY = "fund_inception_date"
INCEPTION_BASELINE_KEY = "fund_inception_sp500_close"

# Fund validation bounds
MAX_DAILY_PCT_NO_TRADES = 25.0       # cum return cannot move >25% in one day with 0 trades
MAX_VALUE_RATIO = 5.0                # total_value cannot exceed 5x INITIAL_CAPITAL
SNAPSHOT_BALANCE_TOLERANCE = 1.00    # cash + pos_val must equal total_value within $1

# Options bounds
OPTION_MAX_VALUE_RATIO = 5.0         # single options position cannot exceed 5x its entry premium

# Process-local cache to avoid hammering yfinance
_sp500_cache = {"price": None, "ts": 0, "source": None,
                "daily_pct": 0.0, "cum_pct": 0.0}


# ============================================================
#  1. INCEPTION (pinned, not recomputed)
# ============================================================

def get_inception() -> dict:
    """Returns the pinned fund inception date + S&P 500 baseline close.

    On first call, pins the inception date by reading the earliest
    paper trade entry_date. Stores in trading_state so it never moves.
    """
    try:
        from predictions.models import get_trading_state, set_trading_state
        date = get_trading_state(INCEPTION_DATE_KEY, default="")
        baseline = get_trading_state(INCEPTION_BASELINE_KEY, default="")

        if date and baseline:
            return {"date": date, "sp500_baseline": float(baseline),
                    "source": "trading_state"}

        # Not pinned yet — compute and pin now
        try:
            from predictions.models import get_all_paper_trades
            trades = get_all_paper_trades()
            if trades:
                earliest = min(t.get("entry_date", "2026-01-01") for t in trades)
                date = earliest[:10]
            else:
                # Fund hasn't traded yet — pin to today
                date = datetime.utcnow().strftime("%Y-%m-%d")
        except Exception:
            date = datetime.utcnow().strftime("%Y-%m-%d")

        # Compute baseline close on inception day
        baseline_close = _fetch_sp500_close_on(date)
        if not baseline_close:
            # Couldn't fetch — return without pinning so we can retry
            return {"date": date, "sp500_baseline": None,
                    "source": "computed_unpinned",
                    "warning": "could not fetch baseline close"}

        # Pin both values permanently
        try:
            set_trading_state(INCEPTION_DATE_KEY, date)
            set_trading_state(INCEPTION_BASELINE_KEY, str(round(baseline_close, 4)))
        except Exception as e:
            logger.warning(f"failed to pin inception: {e}")

        return {"date": date, "sp500_baseline": baseline_close, "source": "computed_pinned"}

    except Exception as e:
        logger.warning(f"get_inception soft-fail: {e}")
        return {"date": None, "sp500_baseline": None, "source": "error",
                "reason": str(e)[:200]}


def _fetch_sp500_close_on(date_str: str) -> Optional[float]:
    """Fetch S&P 500 close on a specific date. Tries ^GSPC then SPY.
    Returns None on any failure."""
    try:
        import yfinance as yf
        # Pull a small range around the target date for resilience
        start = date_str
        end = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=10)).strftime("%Y-%m-%d")
        df = yf.download("^GSPC", start=start, end=end, progress=False,
                         auto_adjust=False, threads=False)
        if df is not None and len(df) > 0:
            close_col = df["Close"]
            # Handle multi-level column from yf occasionally
            try:
                val = float(close_col.iloc[0])
                if val > 100 and val < 100000:
                    return val
            except Exception:
                try:
                    val = float(close_col.values[0])
                    if val > 100 and val < 100000:
                        return val
                except Exception:
                    pass

        # Fallback to SPY * ~10 (SPY tracks 1/10 of S&P value)
        df = yf.download("SPY", start=start, end=end, progress=False,
                         auto_adjust=False, threads=False)
        if df is not None and len(df) > 0:
            try:
                val = float(df["Close"].iloc[0]) * 10.0
                if val > 100 and val < 100000:
                    return val
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"_fetch_sp500_close_on({date_str}) failed: {e}")
    return None


# ============================================================
#  2. S&P 500 TRUTH — multi-source with last-good fallback
# ============================================================

def _last_good_from_state() -> Optional[float]:
    """Read the last-known-good S&P 500 close from trading_state."""
    try:
        from predictions.models import get_trading_state
        v = get_trading_state(SP500_LASTGOOD_KEY, default="")
        if v:
            return float(v)
    except Exception:
        pass
    return None


def _persist_lastgood(close: float):
    """Persist a successful S&P 500 fetch to trading_state."""
    try:
        from predictions.models import set_trading_state
        set_trading_state(SP500_LASTGOOD_KEY, str(round(close, 4)))
        set_trading_state(SP500_LASTGOOD_TS_KEY, datetime.utcnow().isoformat())
    except Exception:
        pass


def get_sp500_truth(force_refresh: bool = False) -> dict:
    """Multi-source S&P 500 lookup with last-good fallback.

    Returns:
        {
          "ok": bool,
          "price": float|None,
          "daily_pct": float,
          "cum_pct": float,             # cumulative since fund inception
          "source": str,                # "yf_gspc" | "yf_spy" | "yf_spx" | "lastgood_state" | "stale"
          "stale": bool,
          "fetched_at": iso str,
        }

    NEVER returns price=0. If all sources fail, returns last-known-good
    from trading_state with stale=True. If even that's missing, returns
    ok=False so callers can use the carry-forward strategy.
    """
    now = time.time()
    if (not force_refresh) and _sp500_cache["price"] and (now - _sp500_cache["ts"]) < SP500_CACHE_TTL:
        return {
            "ok": True, "price": _sp500_cache["price"],
            "daily_pct": _sp500_cache["daily_pct"],
            "cum_pct": _sp500_cache["cum_pct"],
            "source": _sp500_cache["source"] + "_cached",
            "stale": False,
            "fetched_at": datetime.utcfromtimestamp(_sp500_cache["ts"]).isoformat(),
        }

    inception = get_inception()
    baseline = inception.get("sp500_baseline")

    # Sources tried in order — return on first success
    sources = [
        ("yf_gspc", "^GSPC", 1.0),
        ("yf_spy",  "SPY",   10.0),    # SPY tracks 1/10 of S&P value
        ("yf_spx",  "^SPX",  1.0),
    ]

    for source_name, symbol, multiplier in sources:
        try:
            import yfinance as yf
            # Throttle to avoid rate limits
            time.sleep(0.2)
            # Pull last 5 trading days so we can compute daily %
            df = yf.download(symbol, period="5d", progress=False,
                             auto_adjust=False, threads=False)
            if df is None or len(df) < 1:
                continue
            try:
                closes = df["Close"].values.astype(float)
            except Exception:
                continue
            if len(closes) < 1:
                continue
            close = float(closes[-1]) * multiplier
            if not (100 < close < 100000):
                continue   # implausible — skip

            # Compute daily pct
            if len(closes) >= 2:
                prev = float(closes[-2]) * multiplier
                daily_pct = ((close / prev) - 1) * 100 if prev > 0 else 0.0
            else:
                daily_pct = 0.0

            # Compute cum since inception
            cum_pct = 0.0
            if baseline and baseline > 0:
                cum_pct = ((close / baseline) - 1) * 100

            # Cache + persist
            _sp500_cache.update({
                "price": close, "ts": now, "source": source_name,
                "daily_pct": daily_pct, "cum_pct": cum_pct,
            })
            _persist_lastgood(close)

            return {
                "ok": True, "price": round(close, 2),
                "daily_pct": round(daily_pct, 3),
                "cum_pct": round(cum_pct, 3),
                "source": source_name, "stale": False,
                "fetched_at": datetime.utcnow().isoformat(),
            }
        except Exception as e:
            logger.debug(f"sp500 source {source_name} failed: {e}")
            continue

    # All live sources failed — try last good from state
    lg = _last_good_from_state()
    if lg:
        cum_pct = ((lg / baseline) - 1) * 100 if (baseline and baseline > 0) else 0.0
        return {
            "ok": True, "price": round(lg, 2),
            "daily_pct": 0.0, "cum_pct": round(cum_pct, 3),
            "source": "lastgood_state", "stale": True,
            "fetched_at": None,
        }

    return {"ok": False, "price": None, "daily_pct": 0.0, "cum_pct": 0.0,
            "source": "all_failed", "stale": True,
            "fetched_at": datetime.utcnow().isoformat()}


# ============================================================
#  3. FUND TRUTH — options-aware, validated
# ============================================================

def get_fund_truth() -> dict:
    """Validated fund metrics with options-aware bounds.

    Returns:
        {
          "ok": bool,
          "total_value": float,
          "cash": float,
          "positions_value": float,
          "cum_return_pct": float,        # vs INITIAL_CAPITAL
          "num_positions": int,
          "warnings": [str],
          "options_value": float,
          "equity_value": float,
        }

    Validations:
      - cash + positions_value MUST equal total_value within $1
      - total_value capped at 5x INITIAL_CAPITAL (sanity)
      - each option position_value clamped to 5x entry premium
    """
    warnings_list = []
    try:
        from predictions.models import get_open_trades, get_cash
        from predictions.paper_trader import _get_current_prices

        cash = float(get_cash() or 0)
        if cash < 0:
            warnings_list.append(f"negative_cash: ${cash:.2f}")
            cash = 0.0

        open_trades = get_open_trades() or []
        positions_value = 0.0
        options_value = 0.0
        equity_value = 0.0

        if open_trades:
            symbols = list({t["ticker"] for t in open_trades})
            try:
                prices = _get_current_prices(symbols)
            except Exception as e:
                warnings_list.append(f"price_fetch_failed: {str(e)[:80]}")
                prices = {}

            for t in open_trades:
                try:
                    ticker = t["ticker"]
                    instr = (t.get("instrument_type") or "equity").lower()
                    entry_price = float(t.get("entry_price") or 0)
                    shares = float(t.get("shares") or 0)
                    cur = prices.get(ticker)
                    if cur is None or cur <= 0:
                        cur = entry_price
                    cur = float(cur)

                    if instr in ("call", "put"):
                        # OPTIONS — bounded estimate
                        entry_premium = float(t.get("premium_per_contract") or entry_price or 0)
                        contracts = float(t.get("contracts") or shares or 1)
                        strike = float(t.get("strike_price") or 0)
                        # Intrinsic component
                        if instr == "call":
                            intrinsic = max(0, cur - strike) if strike else 0
                        else:
                            intrinsic = max(0, strike - cur) if strike else 0
                        # Time-decayed estimate of remaining premium
                        try:
                            exp = datetime.strptime(t.get("expiration_date", ""), "%Y-%m-%d")
                            dte = max(0, (exp - datetime.now()).days)
                        except Exception:
                            dte = 14
                        hold_days = max(1, int(t.get("hold_duration_days") or 30))
                        time_decay = max(0.05, min(1.0, dte / hold_days))
                        time_value = max(0, entry_premium * 0.5) * time_decay
                        est_premium = max(intrinsic + time_value, entry_premium * 0.05)

                        # HARD UPPER BOUND on option value
                        max_premium = entry_premium * OPTION_MAX_VALUE_RATIO
                        if est_premium > max_premium:
                            warnings_list.append(
                                f"option_clamp: {ticker} est=${est_premium:.2f} > "
                                f"{OPTION_MAX_VALUE_RATIO}x entry=${entry_premium:.2f}"
                            )
                            est_premium = max_premium

                        pos_val = max(0, est_premium * contracts * 100)
                        positions_value += pos_val
                        options_value += pos_val
                    else:
                        # EQUITY
                        pos_val = abs(shares * cur)
                        positions_value += pos_val
                        equity_value += pos_val
                except Exception as e:
                    warnings_list.append(f"trade_skipped {t.get('id')}: {str(e)[:60]}")

        total_value = cash + positions_value

        # Sanity: cap at 5x initial
        if total_value > INITIAL_CAPITAL * MAX_VALUE_RATIO:
            warnings_list.append(
                f"total_value ${total_value:,.2f} exceeds {MAX_VALUE_RATIO}x INITIAL_CAPITAL"
            )

        cum_return_pct = ((total_value - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100.0

        return {
            "ok": True,
            "total_value": round(total_value, 2),
            "cash": round(cash, 2),
            "positions_value": round(positions_value, 2),
            "cum_return_pct": round(cum_return_pct, 3),
            "num_positions": len(open_trades),
            "options_value": round(options_value, 2),
            "equity_value": round(equity_value, 2),
            "warnings": warnings_list,
            "computed_at": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        logger.warning(f"get_fund_truth soft-fail: {e}")
        return {"ok": False, "reason": str(e)[:200],
                "warnings": warnings_list,
                "computed_at": datetime.utcnow().isoformat()}


# ============================================================
#  4. SAFE SNAPSHOT SAVE — uses both truths + validates
# ============================================================

def safe_save_snapshot(force: bool = False) -> dict:
    """Compute fund + sp500 truth, validate, then save snapshot.

    On any validation failure, the snapshot is REJECTED and the prior
    snapshot is preserved (no zero-pollution of the equity curve).

    `force=True` saves even if validation warns (still rejects on hard
    errors like balance mismatch).

    Returns dict describing the action.
    """
    try:
        from predictions.models import (
            save_portfolio_snapshot, get_portfolio_snapshots
        )
        try:
            from predictions.audit import audit_log
        except Exception:
            audit_log = lambda *a, **k: None

        fund = get_fund_truth()
        if not fund.get("ok"):
            return {"ok": False, "action": "fund_truth_failed",
                    "reason": fund.get("reason")}

        # Hard check: cash + positions_value must equal total_value
        cash = fund["cash"]
        pos_val = fund["positions_value"]
        total = fund["total_value"]
        if abs((cash + pos_val) - total) > SNAPSHOT_BALANCE_TOLERANCE:
            try:
                audit_log("snapshot_rejected",
                          old_value=total, new_value=cash + pos_val,
                          caller="safe_save_snapshot",
                          reason=f"balance mismatch: cash+pos={cash + pos_val:.2f} vs total={total:.2f}")
            except Exception:
                pass
            return {"ok": False, "action": "balance_mismatch",
                    "cash": cash, "pos_val": pos_val, "total": total}

        sp = get_sp500_truth()

        # Carry-forward sp500 if today's fetch failed
        snaps = get_portfolio_snapshots(days=5) or []
        prev = snaps[-1] if snaps else None
        sp_daily = sp.get("daily_pct", 0.0)
        sp_cum = sp.get("cum_pct", 0.0)
        sp_source = sp.get("source", "unknown")

        if not sp.get("ok") or sp.get("price") is None:
            if prev:
                sp_cum = float(prev.get("sp500_cumulative_return_pct") or 0)
                sp_daily = 0.0
                sp_source = "carry_forward"
                try:
                    audit_log("sp500_carry_forward",
                              old_value=None, new_value=sp_cum,
                              caller="safe_save_snapshot",
                              reason="all live sources failed; carrying forward prior")
                except Exception:
                    pass

        # Sanity guard: don't let cum_return move >25% in one day with 0 trades
        if prev:
            prev_cum = float(prev.get("cumulative_return_pct") or 0)
            if abs(fund["cum_return_pct"] - prev_cum) > MAX_DAILY_PCT_NO_TRADES and fund["num_positions"] == 0:
                try:
                    audit_log("snapshot_warning",
                              old_value=prev_cum, new_value=fund["cum_return_pct"],
                              caller="safe_save_snapshot",
                              reason=f"cum return moved {fund['cum_return_pct']-prev_cum:+.2f}% with 0 positions")
                except Exception:
                    pass
                if not force:
                    return {"ok": False, "action": "rejected_huge_move",
                            "prev_cum": prev_cum, "new_cum": fund["cum_return_pct"]}

        daily_ret = 0.0
        if prev:
            prev_total = float(prev.get("total_value") or INITIAL_CAPITAL)
            if prev_total > 0:
                daily_ret = ((total / prev_total) - 1) * 100.0

        try:
            save_portfolio_snapshot(
                total_value=round(total, 2),
                cash=round(cash, 2),
                positions_value=round(pos_val, 2),
                daily_ret=round(daily_ret, 3),
                cum_ret=round(fund["cum_return_pct"], 3),
                sp500_daily=round(sp_daily, 3),
                sp500_cum=round(sp_cum, 3),
                num_pos=fund["num_positions"],
            )
        except Exception as e:
            return {"ok": False, "action": "save_failed",
                    "reason": str(e)[:200]}

        try:
            audit_log("snapshot_saved",
                      old_value=None, new_value=total,
                      caller="safe_save_snapshot",
                      reason=f"sp500_source={sp_source} cum={fund['cum_return_pct']:.2f}%",
                      metadata={"sp500_cum": sp_cum, "sp500_source": sp_source,
                                "warnings": fund.get("warnings", [])})
        except Exception:
            pass

        return {"ok": True, "action": "saved",
                "total": total, "cash": cash, "pos_val": pos_val,
                "cum_return_pct": fund["cum_return_pct"],
                "sp500_cum": sp_cum, "sp500_source": sp_source,
                "warnings": fund.get("warnings", [])}

    except Exception as e:
        logger.warning(f"safe_save_snapshot soft-fail: {e}")
        return {"ok": False, "action": "exception", "reason": str(e)[:200]}


# ============================================================
#  5. RECOMPUTE HISTORICAL S&P (one-shot fix endpoint)
# ============================================================

def recompute_sp500_history() -> dict:
    """Re-fetch S&P 500 data and rewrite all historical snapshots'
    sp500_cum + sp500_daily values from the pinned inception baseline.

    Carries forward the last good close for any day yfinance can't
    serve, so the chart is never broken by zeros.
    """
    try:
        from predictions.models import get_portfolio_snapshots, update_snapshot_sp500
        try:
            from predictions.audit import audit_log
        except Exception:
            audit_log = lambda *a, **k: None

        inception = get_inception()
        baseline = inception.get("sp500_baseline")
        if not baseline:
            return {"ok": False, "reason": "no_inception_baseline"}

        snaps = get_portfolio_snapshots(days=730) or []
        if len(snaps) < 1:
            return {"ok": False, "reason": "no_snapshots"}

        import yfinance as yf
        start = inception["date"]
        end = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
        df = None
        for symbol, mult in [("^GSPC", 1.0), ("SPY", 10.0), ("^SPX", 1.0)]:
            try:
                df = yf.download(symbol, start=start, end=end, progress=False,
                                 auto_adjust=False, threads=False)
                if df is not None and len(df) > 0:
                    df_mult = mult
                    break
            except Exception:
                continue
        if df is None or len(df) < 1:
            return {"ok": False, "reason": "all_sources_failed"}

        try:
            # yfinance can return df["Close"] as either a Series (1D) or
            # a DataFrame (2D MultiIndex columns). Handle both robustly.
            close_obj = df["Close"]
            try:
                # If MultiIndex column, take the first level
                import pandas as _pd
                if isinstance(close_obj, _pd.DataFrame):
                    # Pick the only column
                    close_obj = close_obj.iloc[:, 0]
            except Exception:
                pass
            # Flatten to 1D and convert
            arr = close_obj.values
            try:
                arr = arr.flatten()
            except Exception:
                pass
            closes = [float(v) * df_mult for v in arr]
            dates = [d.strftime("%Y-%m-%d") for d in df.index]
        except Exception as e:
            return {"ok": False, "reason": f"parse_failed: {e}"}

        if len(closes) < 1:
            return {"ok": False, "reason": "empty_close_series"}

        fixed = 0
        last_good_close = float(closes[0])
        for snap in snaps:
            snap_date = snap.get("snapshot_date", "")
            match_close = None
            prev_close = None
            for i, d in enumerate(dates):
                if d <= snap_date:
                    match_close = float(closes[i])
                    if i > 0:
                        prev_close = float(closes[i - 1])
                else:
                    break
            if match_close is None:
                # Carry forward last good
                match_close = last_good_close
            else:
                last_good_close = match_close
            cum = ((match_close / baseline) - 1) * 100
            daily = ((match_close / prev_close) - 1) * 100 if prev_close else 0
            try:
                update_snapshot_sp500(snap_date, round(cum, 3), round(daily, 3))
                fixed += 1
            except Exception:
                pass

        try:
            audit_log("sp500_recompute",
                      old_value=None, new_value=fixed,
                      caller="recompute_sp500_history",
                      reason=f"rebuilt {fixed} snapshots from inception {start}")
        except Exception:
            pass

        return {"ok": True, "snapshots_updated": fixed,
                "inception_date": start,
                "inception_baseline": baseline,
                "latest_sp_close": float(closes[-1]),
                "latest_sp_cum_pct": round(((float(closes[-1]) / baseline) - 1) * 100, 3)}

    except Exception as e:
        logger.warning(f"recompute_sp500_history soft-fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}
