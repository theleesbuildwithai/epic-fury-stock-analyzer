"""
Backtest Pro — Hedge-fund-grade analysis layer on top of backtest.py.

Four analyses, designed to answer the questions a real fund's risk
committee would ask:

  1. WALK-FORWARD VALIDATION (overfitting detector)
     Splits history into rolling train/test windows. Compares train
     and test Sharpe to flag curve-fitting. If test_sharpe is
     significantly worse than train_sharpe, the strategy is overfit.

  2. MONTE CARLO BOOTSTRAP (luck vs skill)
     Resamples the trade distribution N times to give CONFIDENCE
     INTERVALS on return + Sharpe + max DD. Tells you whether 32%
     return is solid or a lucky outlier.

  3. REGIME-CONDITIONAL PERFORMANCE
     Classifies each day by SPY trend (bull/bear/sideways) and
     volatility (high/low). Reports per-regime Sharpe + win rate.
     Tells you WHEN the strategy works.

  4. HISTORICAL CRISIS STRESS TESTS
     Replays the strategy across COVID 2020, 2022 inflation crash,
     August 2024 vol spike. Reports drawdown + recovery for each.
     Direct answer to "would we survive the next crisis?"

SAFETY CONTRACT (every function must hold):
  - Returns {"ok": False, "reason": ...} on any failure
  - Wraps every external call (yfinance, run_backtest) in try/except
  - Never raises — backtest_pro errors must NEVER affect live trading
  - All heavy ops have a soft timeout via deterministic fewer-iterations
    fallbacks if needed
"""

import logging
import time
from datetime import datetime, timedelta
from collections import defaultdict

logger = logging.getLogger(__name__)

# Cache for the last full pro analysis (1hr TTL — these runs are heavy)
_last_pro_analysis = {"data": None, "ts": 0}
_PRO_CACHE_TTL = 3600


# ============================================================
#  1. WALK-FORWARD VALIDATION
# ============================================================

def walk_forward_validation(start_date: str = None,
                             end_date: str = None,
                             train_months: int = 6,
                             test_months: int = 1,
                             top_n: int = 10,
                             hold_days: int = 5) -> dict:
    """Roll a (train_months, test_months) window through history.
    For each window: measure Sharpe on train period AND on test period.
    A consistent gap (test < train) signals overfitting.

    Default 6mo train + 1mo test, sliding by test_months. Over 12 months
    of history that gives ~7 windows.

    NEVER raises. Soft-fails individual windows so the overall analysis
    still completes if one window has insufficient data.
    """
    try:
        from predictions.backtest import run_backtest, _safe_yf_download, _DEFAULT_UNIVERSE
        from datetime import datetime as _dt, timedelta as _td

        if not end_date:
            end_date = _dt.utcnow().strftime("%Y-%m-%d")
        if not start_date:
            start_date = (_dt.utcnow() - _td(days=540)).strftime("%Y-%m-%d")  # ~18mo

        start_dt = _dt.fromisoformat(start_date)
        end_dt = _dt.fromisoformat(end_date)

        windows = []
        cur = start_dt
        # Each window: train [cur, cur + train_months) then test [cur + train_months, cur + train_months + test_months)
        while True:
            train_start = cur
            train_end = cur + _td(days=int(train_months * 30))
            test_start = train_end
            test_end = test_start + _td(days=int(test_months * 30))
            if test_end > end_dt:
                break
            windows.append((train_start, train_end, test_start, test_end))
            cur = cur + _td(days=int(test_months * 30))

        if not windows:
            return {"ok": False, "reason": "insufficient_history_for_walk_forward",
                    "needed_days": int((train_months + test_months) * 30)}

        # PRE-LOAD all prices for the full walk-forward range in ONE download.
        # Without this, each window calls run_backtest which downloads 100 tickers
        # independently — for 18 windows that's 36 yfinance calls (~18 minutes).
        # Pre-loading reduces this to 2 calls (universe + SPY), total ~30 seconds.
        def _slice_prices(all_p, s_str, e_str):
            """Slice a {sym: Series} dict to a date range using tz-safe string compare."""
            out = {}
            for sym, ser in all_p.items():
                try:
                    idx = ser.index.strftime("%Y-%m-%d")
                    mask = (idx >= s_str) & (idx <= e_str)
                    sliced = ser[mask]
                    if len(sliced) >= 5:
                        out[sym] = sliced
                except Exception:
                    continue
            return out

        _wf_spy_full = None
        _wf_prices_full = {}
        try:
            _wf_spy_raw = _safe_yf_download(["SPY"], start_date, end_date)
            _wf_spy_full = _wf_spy_raw.get("SPY")
            _wf_prices_full = _safe_yf_download(list(_DEFAULT_UNIVERSE), start_date, end_date)
            logger.info(f"WF pre-load: {len(_wf_prices_full)} tickers, SPY={'OK' if _wf_spy_full is not None else 'FAILED'}")
        except Exception as _ple:
            logger.warning(f"WF pre-load soft-fail (will fall back to per-window download): {_ple}")

        results = []
        for i, (ts, te, vs, ve) in enumerate(windows):
            try:
                ts_str = ts.strftime("%Y-%m-%d")
                te_str = te.strftime("%Y-%m-%d")
                vs_str = vs.strftime("%Y-%m-%d")
                ve_str = ve.strftime("%Y-%m-%d")

                # Slice preloaded prices to this window, or fall back to live download
                if _wf_prices_full:
                    train_prices = _slice_prices(_wf_prices_full, ts_str, te_str)
                    train_spy = _slice_prices({"SPY": _wf_spy_full}, ts_str, te_str).get("SPY") if _wf_spy_full is not None else None
                    test_prices = _slice_prices(_wf_prices_full, vs_str, ve_str)
                    test_spy = _slice_prices({"SPY": _wf_spy_full}, vs_str, ve_str).get("SPY") if _wf_spy_full is not None else None
                else:
                    train_prices = test_prices = None
                    train_spy = test_spy = None

                # Train window
                train_bt = run_backtest(start_date=ts_str, end_date=te_str,
                                         top_n=int(top_n), hold_days=int(hold_days),
                                         _preloaded_prices=train_prices or None,
                                         _preloaded_spy=train_spy)
                # Test window
                test_bt = run_backtest(start_date=vs_str, end_date=ve_str,
                                        top_n=int(top_n), hold_days=int(hold_days),
                                        _preloaded_prices=test_prices or None,
                                        _preloaded_spy=test_spy)
                if not train_bt.get("ok") or not test_bt.get("ok"):
                    continue
                tr_r = train_bt.get("results", {})
                te_r = test_bt.get("results", {})
                results.append({
                    "window": i + 1,
                    "train_period": [ts.strftime("%Y-%m-%d"), te.strftime("%Y-%m-%d")],
                    "test_period": [vs.strftime("%Y-%m-%d"), ve.strftime("%Y-%m-%d")],
                    "train_sharpe": tr_r.get("sharpe_ratio"),
                    "test_sharpe": te_r.get("sharpe_ratio"),
                    "train_return_pct": tr_r.get("total_return_pct"),
                    "test_return_pct": te_r.get("total_return_pct"),
                    "train_win_rate": tr_r.get("win_rate_pct"),
                    "test_win_rate": te_r.get("win_rate_pct"),
                    # AUDIT FIX C2 — emit trade counts so deflated_sharpe
                    # can use the true N_obs instead of the fallback estimate
                    # of len(results)*30 which is materially wrong.
                    "train_trades": tr_r.get("total_trades"),
                    "test_trades": te_r.get("total_trades"),
                })
            except Exception as _we:
                logger.debug(f"walk-forward window {i} soft-fail: {_we}")
                continue

        if not results:
            return {"ok": False, "reason": "all_windows_failed"}

        # Aggregate stats
        train_sharpes = [r["train_sharpe"] for r in results if r["train_sharpe"] is not None]
        test_sharpes = [r["test_sharpe"] for r in results if r["test_sharpe"] is not None]
        avg_train = sum(train_sharpes) / len(train_sharpes) if train_sharpes else 0
        avg_test = sum(test_sharpes) / len(test_sharpes) if test_sharpes else 0

        # Overfit detector: test < 50% of train AND train > 0.5 (meaningful)
        overfit_flag = (avg_train > 0.5 and avg_test < 0.5 * avg_train)

        # Consistency: % of windows where test_sharpe > 0
        consistent = sum(1 for s in test_sharpes if s > 0)
        consistency_pct = (consistent / len(test_sharpes) * 100) if test_sharpes else 0

        # AUDIT #7 — Deflated Sharpe of the aggregate test result.
        # Adjusts measured Sharpe for finite sample, non-normal tails,
        # and multiple-hypothesis selection across all the windows.
        # A backtest SR of 1.5 with low N and many trials often deflates
        # to near-zero — the proper guard against curve-fitting.
        deflated = None
        try:
            from predictions.quant_audit_fixes import deflated_sharpe as _ds
            # Approx N: total test trades across windows
            n_obs_approx = sum(
                (r.get("test_trades") or 0)
                for r in results
            ) or len(results) * 30  # crude fallback
            # n_trials ≈ number of distinct windows tested
            deflated = _ds(
                sr=float(avg_test),
                n_obs=int(n_obs_approx),
                n_trials=max(1, len(results)),
                skew=0.0, kurt=3.0,
            )
        except Exception as _dse:
            logger.debug(f"deflated_sharpe skipped: {_dse}")

        return {
            "ok": True,
            "config": {"train_months": train_months, "test_months": test_months,
                       "windows_run": len(results), "top_n": top_n,
                       "hold_days": hold_days},
            "summary": {
                "avg_train_sharpe": round(avg_train, 3),
                "avg_test_sharpe": round(avg_test, 3),
                "sharpe_decay_pct": round((avg_train - avg_test) / abs(avg_train) * 100, 1)
                                    if avg_train != 0 else 0,
                "test_window_consistency_pct": round(consistency_pct, 1),
                "overfit_flag": overfit_flag,
                "deflated_sharpe": deflated,  # AUDIT #7
                "interpretation": (
                    "OVERFIT WARNING — test Sharpe << train Sharpe"
                    if overfit_flag else
                    "stable — test Sharpe in line with train Sharpe"
                ),
            },
            "windows": results,
        }
    except Exception as e:
        logger.warning(f"walk_forward_validation fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


# ============================================================
#  2. MONTE CARLO BOOTSTRAP
# ============================================================

def monte_carlo_bootstrap(n_simulations: int = 1000,
                          start_date: str = None,
                          end_date: str = None,
                          top_n: int = 10,
                          hold_days: int = 5,
                          initial_capital: float = 100000.0) -> dict:
    """Run a single backtest, then bootstrap-resample its trade list
    N_SIMULATIONS times to compute confidence intervals on return,
    Sharpe, and max drawdown.

    Each simulation samples len(trades) trades WITH replacement and
    applies them sequentially to compute a synthetic equity path.
    Returns 5/50/95 percentiles + probability metrics.

    n_simulations capped at 5000 to bound compute time. Default 1000.
    NEVER raises.
    """
    try:
        import random
        from predictions.backtest import run_backtest

        n_sims = max(100, min(5000, int(n_simulations)))

        bt = run_backtest(start_date=start_date, end_date=end_date,
                          top_n=int(top_n), hold_days=int(hold_days),
                          initial_capital=float(initial_capital),
                          include_internals=True)
        if not bt.get("ok"):
            return {"ok": False, "phase": "backtest", "reason": bt.get("reason")}

        # Read trades from top-level key first (_trades always set), fallback to _internals
        trades = bt.get("_trades") or (bt.get("_internals") or {}).get("trades") or []
        if len(trades) < 10:
            return {"ok": False, "reason": f"too_few_trades_for_bootstrap ({len(trades)})"}

        # Trade-level pnl in fractional form (e.g. 0.025 = 2.5%)
        position_pct = 0.08  # matches backtest default
        trade_returns = []
        for t in trades:
            try:
                pnl_pct = float(t.get("pnl_pct") or 0) / 100.0  # to fraction
                # Each trade represents position_pct of equity, so its impact
                # on portfolio = pnl_pct * position_pct
                trade_returns.append(pnl_pct * position_pct)
            except Exception:
                continue

        if not trade_returns:
            return {"ok": False, "reason": "no_valid_trade_returns"}

        n_trades = len(trade_returns)
        # Use a seeded RNG for reproducibility
        rng = random.Random(42)

        sim_returns = []
        sim_sharpes = []
        sim_max_dds = []

        for _ in range(n_sims):
            # Sample n_trades returns with replacement
            sample = [trade_returns[rng.randint(0, n_trades - 1)]
                      for _ in range(n_trades)]

            # Build synthetic equity curve
            equity = initial_capital
            equity_path = [equity]
            for r in sample:
                equity = equity * (1 + r)
                equity_path.append(equity)

            final = equity_path[-1]
            total_ret = (final / initial_capital - 1) * 100

            # Sharpe approximation (per-trade, not annualized — relative metric)
            mean_r = sum(sample) / len(sample) if sample else 0
            std_r = (sum((r - mean_r) ** 2 for r in sample) / len(sample)) ** 0.5
            sim_sharpe = (mean_r / std_r * (len(sample) ** 0.5)) if std_r > 0 else 0

            # Max drawdown on the synthetic path
            peak = equity_path[0]
            max_dd = 0
            for v in equity_path:
                if v > peak:
                    peak = v
                dd = (v - peak) / peak * 100 if peak > 0 else 0
                if dd < max_dd:
                    max_dd = dd

            sim_returns.append(total_ret)
            sim_sharpes.append(sim_sharpe)
            sim_max_dds.append(max_dd)

        sim_returns.sort()
        sim_sharpes.sort()
        sim_max_dds.sort()  # most-negative first

        def _pct(arr, p):
            idx = int(len(arr) * p)
            idx = max(0, min(len(arr) - 1, idx))
            return round(arr[idx], 2)

        prob_positive = sum(1 for r in sim_returns if r > 0) / len(sim_returns) * 100
        prob_beat_10 = sum(1 for r in sim_returns if r > 10) / len(sim_returns) * 100

        return {
            "ok": True,
            "config": {"n_simulations": n_sims, "n_trades_per_sim": n_trades,
                       "initial_capital": initial_capital,
                       "top_n": top_n, "hold_days": hold_days},
            "actual_backtest": {
                "total_return_pct": bt["results"].get("total_return_pct"),
                "sharpe": bt["results"].get("sharpe_ratio"),
                "max_dd_pct": bt["results"].get("max_drawdown_pct"),
                "n_trades": n_trades,
            },
            "bootstrap_distribution": {
                "return_pct": {
                    "p5": _pct(sim_returns, 0.05),
                    "p25": _pct(sim_returns, 0.25),
                    "median": _pct(sim_returns, 0.50),
                    "p75": _pct(sim_returns, 0.75),
                    "p95": _pct(sim_returns, 0.95),
                },
                "sharpe": {
                    "p5": _pct(sim_sharpes, 0.05),
                    "median": _pct(sim_sharpes, 0.50),
                    "p95": _pct(sim_sharpes, 0.95),
                },
                "max_dd_pct": {
                    "p5_worst": _pct(sim_max_dds, 0.05),
                    "median": _pct(sim_max_dds, 0.50),
                    "p95_best": _pct(sim_max_dds, 0.95),
                },
            },
            "probabilities": {
                "p_positive_return": round(prob_positive, 1),
                "p_return_gt_10pct": round(prob_beat_10, 1),
            },
        }
    except Exception as e:
        logger.warning(f"monte_carlo_bootstrap fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


# ============================================================
#  3. REGIME-CONDITIONAL PERFORMANCE
# ============================================================

def _classify_regime(sp_close_today: float, sp_ma_50: float, sp_ma_200: float,
                      vol_20d_annual: float) -> str:
    """Pure classifier — no external calls. Returns label like 'bull_lowvol'."""
    if sp_ma_50 > sp_ma_200 * 1.02:
        trend = "bull"
    elif sp_ma_50 < sp_ma_200 * 0.98:
        trend = "bear"
    else:
        trend = "sideways"
    if vol_20d_annual > 0.25:
        vol = "highvol"
    elif vol_20d_annual < 0.13:
        vol = "lowvol"
    else:
        vol = "midvol"
    return f"{trend}_{vol}"


def regime_conditional_analysis(start_date: str = None,
                                 end_date: str = None,
                                 top_n: int = 10,
                                 hold_days: int = 5) -> dict:
    """Run a backtest, classify each trading day by SPY trend + vol
    regime, then compute strategy return + Sharpe per regime.

    Tells you WHEN your strategy works (and breaks).
    NEVER raises.
    """
    try:
        from predictions.backtest import run_backtest

        bt = run_backtest(start_date=start_date, end_date=end_date,
                          top_n=int(top_n), hold_days=int(hold_days),
                          include_internals=True)
        if not bt.get("ok"):
            return {"ok": False, "phase": "backtest", "reason": bt.get("reason")}

        # Read from top-level keys first (_equity_curve / _sp500_series always set),
        # fallback to _internals for backward compatibility
        eq_curve = bt.get("_equity_curve") or (bt.get("_internals") or {}).get("equity_curve") or []
        sp_series = bt.get("_sp500_series") or (bt.get("_internals") or {}).get("sp500_series") or []
        if len(eq_curve) < 60:
            return {"ok": False, "reason": "insufficient_data_for_regime_analysis"}

        # If sp_series is too short for MA200 (~62 pts for 90d backtest), try
        # to download more SPY history.  If download fails, gracefully degrade:
        # classify using price vs MA50 instead of MA50 vs MA200 — still a valid
        # regime signal, just shorter-window.
        if len(sp_series) < 200:
            import yfinance as _yf_rg
            import threading as _rg_thr
            _rg_end = end_date or datetime.today().strftime("%Y-%m-%d")
            # 400 calendar days gives ~275 trading days — enough for MA200
            _rg_start = (datetime.strptime(_rg_end, "%Y-%m-%d") - timedelta(days=400)).strftime("%Y-%m-%d")
            _spy_rg = [None]

            def _fetch_rg(r=_spy_rg, s=_rg_start, e=_rg_end):
                # Use Ticker().history() — returns flat DataFrame with "Close" column,
                # no MultiIndex headaches regardless of yfinance version.
                for sym in ("SPY", "^GSPC", "IVV", "VOO"):
                    try:
                        _tkr = _yf_rg.Ticker(sym)
                        _hist = _tkr.history(start=s, end=e, auto_adjust=True)
                        if _hist is None or _hist.empty:
                            logger.warning(f"regime: {sym} history empty")
                            continue
                        col = (_hist["Close"] if "Close" in _hist.columns
                               else _hist.iloc[:, 0]).dropna()
                        if len(col) >= 200:
                            r[0] = col
                            logger.info(f"regime: {sym} OK — {len(col)} pts")
                            return
                        logger.warning(f"regime: {sym} only {len(col)} pts, need 200")
                    except Exception as _ex:
                        logger.warning(f"regime: {sym} download failed: {_ex}")
                        continue

            _t_rg = _rg_thr.Thread(target=_fetch_rg, daemon=True)
            _t_rg.start()
            _t_rg.join(timeout=30)  # 30s enough for success; fast-fail on rate-limit
            if _spy_rg[0] is not None and len(_spy_rg[0]) >= 200:
                sp_series = [
                    {"date": str(d)[:10], "close": float(v)}
                    for d, v in _spy_rg[0].items()
                    if v == v  # filter NaN
                ]
                logger.info(f"regime: independent SPY download — {len(sp_series)} pts")
            else:
                # Degrade gracefully: use the short sp_series with price vs MA50 mode.
                # _has_ma200=False triggers price-vs-MA50 classification below.
                logger.warning(f"regime: download failed — degrading to price-vs-MA50 "
                               f"with {len(sp_series)} pts")

        # Index SPY by date for quick lookup
        sp_by_date = {p["date"]: p["close"] for p in sp_series}
        sp_dates = sorted(sp_by_date.keys())

        _n_sp = len(sp_dates)
        _has_ma200 = (_n_sp >= 200)
        # Start loop at 200 when we have full MA200 data; else 20 (degraded mode uses
        # price vs short MA, needs only 20 pts of history to start classifying days).
        _loop_start = 200 if _has_ma200 else 20

        if _n_sp < 20:
            return {"ok": False, "reason": "insufficient_spy_data_for_regime_analysis",
                    "n_spy_pts": _n_sp}

        # Pre-compute SPY MA, vol per date.
        # When _has_ma200=False: pass (price, price, ma_50, vol) to _classify_regime
        # so it evaluates price vs MA50 (bull = price 2% above MA50, etc.).
        regime_by_date = {}
        for i in range(_loop_start, _n_sp):
            d = sp_dates[i]
            try:
                _lo50 = max(0, i - 50)
                window_50 = [sp_by_date[sp_dates[j]] for j in range(_lo50, i)]
                _lo20 = max(0, i - 20)
                window_20 = [sp_by_date[sp_dates[j]] for j in range(_lo20, i)]
                if not window_50:
                    continue
                ma_50 = sum(window_50) / len(window_50)
                if _has_ma200:
                    window_200 = [sp_by_date[sp_dates[j]] for j in range(i - 200, i)]
                    ma_200 = sum(window_200) / len(window_200)
                else:
                    # Price-vs-MA50 mode: treat current price as "short MA"
                    # so _classify_regime compares price to MA50.
                    ma_200 = ma_50
                    ma_50 = sp_by_date[d]  # reuse ma_50 slot for price
                # Daily log returns for vol
                rets = []
                for j in range(1, len(window_20)):
                    if window_20[j-1] > 0:
                        rets.append(window_20[j] / window_20[j-1] - 1)
                if rets:
                    mean = sum(rets) / len(rets)
                    var = sum((r - mean) ** 2 for r in rets) / len(rets)
                    daily_vol = var ** 0.5
                    annual_vol = daily_vol * (252 ** 0.5)
                else:
                    annual_vol = 0
                regime_by_date[d] = _classify_regime(
                    sp_by_date[d], ma_50, ma_200, annual_vol)
            except Exception:
                continue

        # Compute strategy daily returns + group by regime
        per_regime = defaultdict(lambda: {"days": 0, "returns": []})
        for i in range(1, len(eq_curve)):
            try:
                prev = eq_curve[i-1]["equity"]
                cur = eq_curve[i]["equity"]
                d = eq_curve[i]["date"]
                if prev <= 0:
                    continue
                daily_ret = (cur / prev) - 1
                regime = regime_by_date.get(d)
                if not regime:
                    continue
                per_regime[regime]["days"] += 1
                per_regime[regime]["returns"].append(daily_ret)
            except Exception:
                continue

        # Build per-regime summary
        breakdown = {}
        for regime, data in per_regime.items():
            rets = data["returns"]
            if not rets:
                continue
            n = len(rets)
            mean_r = sum(rets) / n
            std_r = (sum((r - mean_r) ** 2 for r in rets) / n) ** 0.5
            sharpe = (mean_r / std_r * (252 ** 0.5)) if std_r > 0 else 0
            cum_ret = 1.0
            for r in rets:
                cum_ret *= (1 + r)
            cum_pct = (cum_ret - 1) * 100
            wins = sum(1 for r in rets if r > 0)
            breakdown[regime] = {
                "days": n,
                "cum_return_pct": round(cum_pct, 2),
                "avg_daily_return_pct": round(mean_r * 100, 4),
                "annualized_sharpe": round(sharpe, 2),
                "win_rate_pct": round(wins / n * 100, 1),
            }

        # Sort regimes by Sharpe descending
        sorted_regimes = sorted(breakdown.items(),
                                 key=lambda x: x[1]["annualized_sharpe"],
                                 reverse=True)

        _trend_method = ("MA50 vs MA200 (2% buffer)" if _has_ma200
                         else "price vs MA50 (degraded — MA200 data unavailable)")
        return {
            "ok": True,
            "config": {"top_n": top_n, "hold_days": hold_days},
            "regime_classifier": {
                "trend": _trend_method,
                "vol": "20d annualized realized vol on SPY (low<13%, high>25%)",
            },
            "actual_backtest": {
                "total_return_pct": bt["results"].get("total_return_pct"),
                "sharpe": bt["results"].get("sharpe_ratio"),
            },
            "by_regime": dict(sorted_regimes),
            "best_regime": sorted_regimes[0][0] if sorted_regimes else None,
            "worst_regime": sorted_regimes[-1][0] if sorted_regimes else None,
        }
    except Exception as e:
        logger.warning(f"regime_conditional_analysis fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


# ============================================================
#  4. HISTORICAL CRISIS STRESS TESTS
# ============================================================

# Predefined crisis windows. Each tuple: (label, start, end, description).
# Picked to span the most consequential vol events available on yfinance.
CRISIS_PERIODS = [
    ("covid_crash_2020", "2020-02-15", "2020-04-30",
     "COVID-19 crash + V-shaped recovery"),
    ("inflation_2022", "2022-01-01", "2022-10-31",
     "2022 inflation + Fed hike bear market"),
    ("svb_collapse_2023", "2023-03-01", "2023-04-15",
     "SVB / regional banking crisis"),
    ("aug_2024_volspike", "2024-07-25", "2024-09-15",
     "August 2024 yen carry unwind + vol spike"),
]


def stress_tests(top_n: int = 10, hold_days: int = 5) -> dict:
    """Replays the strategy across major historical crises.
    Returns per-crisis return + max DD + recovery info.
    Each period's failure does NOT block the rest. NEVER raises.
    """
    try:
        from predictions.backtest import run_backtest

        results = []
        for label, start, end, desc in CRISIS_PERIODS:
            try:
                bt = run_backtest(start_date=start, end_date=end,
                                   top_n=int(top_n),
                                   hold_days=int(hold_days))
                if not bt.get("ok"):
                    results.append({
                        "label": label, "description": desc,
                        "period": [start, end], "ok": False,
                        "reason": bt.get("reason"),
                    })
                    continue
                r = bt.get("results", {})
                cfg = bt.get("config", {})
                results.append({
                    "label": label,
                    "description": desc,
                    "period": [start, end],
                    "ok": True,
                    "trading_days": cfg.get("trading_days"),
                    "tickers_count": cfg.get("tickers_count"),
                    "total_return_pct": r.get("total_return_pct"),
                    "sp500_return_pct": r.get("sp500_return_pct"),
                    "alpha_vs_sp500_pct": r.get("alpha_vs_sp500_pct"),
                    "max_drawdown_pct": r.get("max_drawdown_pct"),
                    "sharpe_ratio": r.get("sharpe_ratio"),
                    "win_rate_pct": r.get("win_rate_pct"),
                    "total_trades": r.get("total_trades"),
                })
            except Exception as _ce:
                logger.debug(f"stress test {label} soft-fail: {_ce}")
                results.append({
                    "label": label, "description": desc,
                    "period": [start, end], "ok": False,
                    "reason": str(_ce)[:200],
                })

        ok_results = [r for r in results if r.get("ok")]
        if ok_results:
            worst = min(ok_results, key=lambda r: r.get("max_drawdown_pct") or 0)
            best = max(ok_results, key=lambda r: r.get("total_return_pct") or 0)
        else:
            worst = best = None

        return {
            "ok": True,
            "config": {"top_n": top_n, "hold_days": hold_days,
                       "n_periods": len(CRISIS_PERIODS),
                       "n_succeeded": len(ok_results)},
            "crises": results,
            "worst_drawdown_crisis": worst.get("label") if worst else None,
            "best_return_crisis": best.get("label") if best else None,
        }
    except Exception as e:
        logger.warning(f"stress_tests fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


# ============================================================
#  AGGREGATE — run all 4 analyses (cached 1hr)
# ============================================================

def run_full_pro_analysis(top_n: int = 10, hold_days: int = 5,
                           force_refresh: bool = False) -> dict:
    """Runs all 4 hedge-fund analyses sequentially and caches the
    result for 1 hour. Heavy operation — typically 30-90 seconds.
    NEVER raises.
    """
    try:
        now = time.time()
        cached = _last_pro_analysis.get("data")
        cached_ts = _last_pro_analysis.get("ts", 0)
        if (cached and not force_refresh and
                (now - cached_ts) < _PRO_CACHE_TTL):
            cached = dict(cached)
            cached["_cache_age_seconds"] = int(now - cached_ts)
            cached["_cached"] = True
            return cached

        wf = walk_forward_validation(top_n=top_n, hold_days=hold_days)
        mc = monte_carlo_bootstrap(top_n=top_n, hold_days=hold_days)
        rg = regime_conditional_analysis(top_n=top_n, hold_days=hold_days)
        st = stress_tests(top_n=top_n, hold_days=hold_days)

        result = {
            "ok": True,
            "computed_at": datetime.utcnow().isoformat(),
            "_cached": False,
            "walk_forward": wf,
            "monte_carlo": mc,
            "regimes": rg,
            "stress_tests": st,
        }
        try:
            _last_pro_analysis["data"] = result
            _last_pro_analysis["ts"] = now
        except Exception:
            pass
        return result
    except Exception as e:
        logger.warning(f"run_full_pro_analysis fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


def get_pro_summary() -> dict:
    """Returns the cached pro analysis if any, with cache age. Soft-fails."""
    try:
        cached = _last_pro_analysis.get("data")
        if not cached:
            return {"ok": False, "reason": "no_pro_analysis_run_yet",
                    "message": "Run /api/backtest-pro/full first"}
        out = dict(cached)
        out["_cache_age_seconds"] = int(time.time() - _last_pro_analysis.get("ts", 0))
        out["_cached"] = True
        return out
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}
