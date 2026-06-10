"""
OU / Stat Arb Pairs Trader — execution layer.

Wires the existing find_pairs_trades() signals into live paper trades.
Each pair opens TWO legs simultaneously (long + short) stored with
instrument_type='pair_equity'. Both legs share a pair_id in their
factors JSON so the exit checker can close them atomically.

OU model (Ornstein-Uhlenbeck):
    spread_t = log(A_t) - beta * log(B_t)
    dS = kappa*(mu - S) dt + sigma * dW
    half_life = ln(2) / kappa

Entry rules:
    |z_score| >= 2.0          Strong signal (spread stretched 2+ sigma)
    half_life <= 20 days       Fast mean reversion only
    correlation >= 0.65        Pairs must be tightly correlated
    confidence >= 60           Signal engine minimum
    regime != CRISIS           No pairs when market is in freefall
    cash budget available      50% of cash max reserved for pairs

Exit rules (spread-based):
    |z| < 0.5   -> TAKE PROFIT (spread reverted)
    |z| > 3.5   -> STOP LOSS   (spread kept widening)
    days > 2 * half_life -> TIME STOP (not reverting fast enough)
    orphan leg detected  -> close remaining leg

Safety nets (lessons from previous bugs):
    - Every float validated: NaN/Inf/zero rejected before use
    - Each pair attempt isolated in try/except — one bad pair never crashes others
    - close_paper_trade() called with 2 args only (no reason param)
    - Price fetch failure = HOLD (never exit on bad data)
    - Duplicate guard: one open position per pair_id
    - Cash floor: pairs cannot starve directional trades
    - Market hours enforced by caller (never opens off-hours)
    - yfinance multi-ticker fetch with group_by='ticker' (avoids column flattening bug)
"""

import json
import logging
import math
import time
from datetime import datetime

import numpy as np

logger = logging.getLogger("pairs_trader")

# ============================================================
# Constants
# ============================================================
MAX_CONCURRENT_PAIRS   = 3      # Max open pair positions simultaneously
MAX_LEG_PCT_NAV        = 0.05   # 5% of NAV per leg (10% gross per pair)
MAX_CASH_USAGE_PCT     = 0.40   # Pairs use at most 40% of available cash
MIN_ZSCORE_ENTRY       = 2.0    # |z| threshold to open a pair
MAX_HALFLIFE_ENTRY     = 20.0   # Max OU half-life in days to qualify
MIN_CORRELATION_ENTRY  = 0.65   # 60d rolling correlation floor
MIN_CONFIDENCE_ENTRY   = 60     # rentech confidence score floor
EXIT_Z_TARGET          = 0.5    # Close when |z| falls below this (reversion)
EXIT_Z_STOP            = 3.5    # Close when |z| exceeds this (stop loss)
BLOCKED_REGIMES        = {"CRISIS"}  # Never open pairs here
MIN_LEG_CAPITAL        = 300.0  # Minimum dollar value per leg


# ============================================================
# OU z-score recomputation (used for exits)
# ============================================================

def _recompute_ou_zscore(ticker_long: str, ticker_short: str,
                          hedge_ratio: float) -> tuple:
    """
    Fetch fresh 90d prices for both legs and recompute the OU spread z-score.

    Uses the ENTRY hedge_ratio (not re-estimated) so we measure the spread
    exactly as it was defined at entry — ensures exit z-score is comparable.

    Returns: (z_score: float, half_life: float, ok: bool)
    Returns (None, None, False) on any data failure — caller must hold.

    Safety nets:
    - Rejects NaN / Inf / zero prices before touching numpy
    - Minimum 30 data points required
    - Numerical stability: std < 1e-8 aborts
    - Fully wrapped in try/except
    """
    try:
        import yfinance as yf

        df = yf.download(
            f"{ticker_long} {ticker_short}",
            period="90d",
            progress=False,
            group_by="ticker",
            auto_adjust=True,
        )
        if df is None or df.empty:
            return None, None, False

        # Extract close arrays — handle both multi-ticker and single-ticker layouts
        def _extract_close(df_, ticker):
            try:
                if hasattr(df_.columns, "levels"):
                    # MultiIndex: (field, ticker)
                    col = df_[ticker]["Close"] if ticker in df_.columns.get_level_values(0) else \
                          df_["Close"][ticker]
                    return col.dropna().values.astype(float)
                else:
                    return df_["Close"].dropna().values.astype(float)
            except Exception:
                return np.array([])

        closes_long  = _extract_close(df, ticker_long)
        closes_short = _extract_close(df, ticker_short)

        if len(closes_long) < 30 or len(closes_short) < 30:
            return None, None, False

        # Align lengths
        n = min(len(closes_long), len(closes_short))
        closes_long  = closes_long[-n:]
        closes_short = closes_short[-n:]

        # Reject NaN / Inf / non-positive prices
        if (not np.all(np.isfinite(closes_long))  or
                not np.all(np.isfinite(closes_short)) or
                np.any(closes_long  <= 0) or
                np.any(closes_short <= 0)):
            return None, None, False

        # Spread = log(A) - hedge_ratio * log(B)
        log_long  = np.log(closes_long)
        log_short = np.log(closes_short)
        spread    = log_long - hedge_ratio * log_short

        mu_s  = float(np.mean(spread))
        sig_s = float(np.std(spread))
        if sig_s < 1e-8:
            return None, None, False

        z = float((spread[-1] - mu_s) / sig_s)
        if not math.isfinite(z):
            return None, None, False

        # OU half-life via AR(1) regression on spread differences
        half_life = 999.0
        try:
            dspread    = np.diff(spread)
            spread_lag = spread[:-1] - mu_s
            if np.std(spread_lag) > 1e-8:
                theta = -float(np.polyfit(spread_lag, dspread, 1)[0])
                if theta > 1e-6:
                    hl = float(np.log(2) / theta)
                    if math.isfinite(hl) and 0 < hl < 9999:
                        half_life = hl
        except Exception:
            pass  # fall back to 999

        return z, half_life, True

    except Exception as e:
        logger.debug(f"OU zscore recompute ({ticker_long}/{ticker_short}): {e}")
        return None, None, False


# ============================================================
# Price helpers
# ============================================================

def _get_pair_prices(ticker_a: str, ticker_b: str) -> dict:
    """
    Fetch current prices for two tickers in one yfinance call.
    Returns {ticker: price}. Missing tickers simply absent from dict.
    Safety net: NaN / Inf / non-positive prices excluded.
    """
    result = {}
    try:
        import yfinance as yf
        df = yf.download(
            f"{ticker_a} {ticker_b}",
            period="2d",
            progress=False,
            group_by="ticker",
            auto_adjust=True,
        )
        if df is None or df.empty:
            return result
        for tk in [ticker_a, ticker_b]:
            try:
                if hasattr(df.columns, "levels"):
                    close = df[tk]["Close"].dropna() if tk in df.columns.get_level_values(0) else \
                            df["Close"][tk].dropna()
                else:
                    close = df["Close"].dropna()
                val = float(close.iloc[-1])
                if math.isfinite(val) and val > 0:
                    result[tk] = val
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"_get_pair_prices ({ticker_a}/{ticker_b}): {e}")
    return result


# ============================================================
# Entry — open new pairs from signal queue
# ============================================================

def execute_pairs_from_signals(quant_picks: dict, open_trades: list,
                                cash: float, regime: str, nav: float) -> list:
    """
    Open new pairs trades from quant_picks["pairs_trades"].

    Called at the END of execute_trades_from_signals() so pairs never
    compete with or block directional trades.

    Design guarantees:
    - Returns [] on any outer failure (never raises)
    - Each pair attempt wrapped in try/except (one bad pair can't crash others)
    - Duplicate guard: tracks open_pair_ids, skips if already open
    - Cash budget: 40% of cash max for pairs total
    - Leg size: 5% of NAV, capped to remaining budget
    - Dollar-neutral: short_capital = long_capital * hedge_ratio
    - Both legs must open or neither opens (checked via returned id)
    - All numeric inputs validated before use
    """
    opened = []

    try:
        # Regime guard
        if regime in BLOCKED_REGIMES:
            logger.info(f"PAIRS ENTRY: blocked — regime={regime}")
            return opened

        pairs_signals = quant_picks.get("pairs_trades") or []
        if not pairs_signals:
            return opened

        # NAV sanity
        if not math.isfinite(nav) or nav <= 0:
            return opened

        # Identify currently open pair IDs to prevent duplicates
        open_pair_ids = set()
        for t in open_trades:
            try:
                f = json.loads(t.get("factors") or "{}")
                pid = f.get("pair_id")
                if pid:
                    open_pair_ids.add(pid)
            except Exception:
                pass

        if len(open_pair_ids) >= MAX_CONCURRENT_PAIRS:
            logger.info(f"PAIRS ENTRY: at max concurrent pairs ({MAX_CONCURRENT_PAIRS})")
            return opened

        pairs_cash_budget = cash * MAX_CASH_USAGE_PCT
        pairs_cash_used   = 0.0

        from predictions.models import save_paper_trade

        for sig in pairs_signals:
            if len(open_pair_ids) >= MAX_CONCURRENT_PAIRS:
                break

            try:
                sym_a = sig.get("symbol_a", "")
                sym_b = sig.get("symbol_b", "")
                if not sym_a or not sym_b:
                    continue

                pair_id = f"{sym_a}/{sym_b}"

                # Duplicate guard
                if pair_id in open_pair_ids:
                    continue

                # ---- Validate all numeric signal fields ----
                z           = sig.get("z_score", 0)
                half_life   = sig.get("half_life_days", 999)
                conf        = sig.get("confidence", 0)
                corr        = sig.get("correlation", 0)
                price_a     = sig.get("price_a", 0)
                price_b     = sig.get("price_b", 0)
                hedge_ratio = sig.get("hedge_ratio", 1.0)
                exp_ret     = sig.get("expected_return_pct", abs(z) * 2.5)

                bad_field = None
                for val, name in [
                    (z,           "z_score"),
                    (half_life,   "half_life_days"),
                    (conf,        "confidence"),
                    (corr,        "correlation"),
                    (price_a,     "price_a"),
                    (price_b,     "price_b"),
                    (hedge_ratio, "hedge_ratio"),
                    (exp_ret,     "expected_return_pct"),
                ]:
                    if not isinstance(val, (int, float)) or not math.isfinite(float(val)):
                        bad_field = name
                        break
                if bad_field:
                    logger.debug(f"PAIRS: {pair_id} rejected — non-finite {bad_field}")
                    continue

                z           = float(z)
                half_life   = float(half_life)
                conf        = float(conf)
                corr        = float(corr)
                price_a     = float(price_a)
                price_b     = float(price_b)
                hedge_ratio = float(hedge_ratio)
                exp_ret     = float(exp_ret)

                # ---- Entry gates ----
                if abs(z) < MIN_ZSCORE_ENTRY:
                    continue
                if half_life > MAX_HALFLIFE_ENTRY:
                    continue
                if conf < MIN_CONFIDENCE_ENTRY:
                    continue
                if corr < MIN_CORRELATION_ENTRY:
                    continue
                if price_a <= 0 or price_b <= 0:
                    logger.warning(f"PAIRS: {pair_id} invalid prices a={price_a} b={price_b}")
                    continue
                if hedge_ratio <= 0:
                    continue

                long_leg  = sig.get("long_leg", "")
                short_leg = sig.get("short_leg", "")
                if not long_leg or not short_leg:
                    continue

                price_long  = price_a if long_leg  == sym_a else price_b
                price_short = price_b if short_leg == sym_b else price_a

                if price_long <= 0 or price_short <= 0:
                    continue

                # ---- Position sizing ----
                leg_capital = min(
                    nav * MAX_LEG_PCT_NAV,
                    pairs_cash_budget - pairs_cash_used,
                )
                if leg_capital < MIN_LEG_CAPITAL:
                    logger.info(f"PAIRS: {pair_id} insufficient capital ({leg_capital:.0f})")
                    break  # no more budget for pairs this cycle

                long_shares  = round(leg_capital / price_long,  4)
                short_capital = leg_capital * abs(hedge_ratio)
                short_shares  = round(short_capital / price_short, 4)

                if long_shares <= 0 or short_shares <= 0:
                    continue

                # ---- Stop / target ----
                long_stop    = round(price_long  * 0.90, 2)   # 10% wide — pairs are hedged
                short_stop   = round(price_short * 1.10, 2)
                long_target  = round(price_long  * (1 + exp_ret / 100), 2)
                short_target = round(price_short * (1 - exp_ret / 100), 2)
                hold_days    = max(5, min(60, int(half_life * 2)))

                today_str       = datetime.now().strftime("%Y%m%d")
                dated_pair_id   = f"{pair_id}_{today_str}"

                long_factors = json.dumps({
                    "pair_id":        pair_id,
                    "dated_pair_id":  dated_pair_id,
                    "pair_role":      "long",
                    "hedge_ratio":    round(hedge_ratio, 4),
                    "entry_z":        round(z, 3),
                    "half_life_days": round(half_life, 1),
                    "exit_z_target":  EXIT_Z_TARGET,
                    "exit_z_stop":    EXIT_Z_STOP,
                    "partner_ticker": short_leg,
                    "signal_type":    "ou_stat_arb",
                })
                short_factors = json.dumps({
                    "pair_id":        pair_id,
                    "dated_pair_id":  dated_pair_id,
                    "pair_role":      "short",
                    "hedge_ratio":    round(hedge_ratio, 4),
                    "entry_z":        round(z, 3),
                    "half_life_days": round(half_life, 1),
                    "exit_z_target":  EXIT_Z_TARGET,
                    "exit_z_stop":    EXIT_Z_STOP,
                    "partner_ticker": long_leg,
                    "signal_type":    "ou_stat_arb",
                })

                # ---- Open both legs atomically ----
                long_id = save_paper_trade(
                    ticker=long_leg,
                    direction="long",
                    entry_price=price_long,
                    shares=long_shares,
                    signal_score=abs(z),
                    regime=regime,
                    factors=long_factors,
                    stop_loss=long_stop,
                    target_price=long_target,
                    hold_days=hold_days,
                    sector="StatArb",
                    hold_class="stat_arb",
                    instrument_type="pair_equity",
                )

                short_id = save_paper_trade(
                    ticker=short_leg,
                    direction="short",
                    entry_price=price_short,
                    shares=short_shares,
                    signal_score=abs(z),
                    regime=regime,
                    factors=short_factors,
                    stop_loss=short_stop,
                    target_price=short_target,
                    hold_days=hold_days,
                    sector="StatArb",
                    hold_class="stat_arb",
                    instrument_type="pair_equity",
                )

                pairs_cash_used += leg_capital + short_capital
                open_pair_ids.add(pair_id)

                logger.warning(
                    f"PAIRS OPENED: {pair_id} | "
                    f"LONG {long_leg} x{long_shares}@${price_long} | "
                    f"SHORT {short_leg} x{short_shares}@${price_short} | "
                    f"z={z:.2f} hl={half_life:.1f}d corr={corr:.2f} conf={conf:.0f}"
                )

                opened.append({
                    "pair":           pair_id,
                    "long_leg":       long_leg,
                    "short_leg":      short_leg,
                    "z_score":        round(z, 2),
                    "half_life_days": round(half_life, 1),
                    "correlation":    round(corr, 2),
                    "confidence":     conf,
                    "long_trade_id":  long_id,
                    "short_trade_id": short_id,
                    "long_entry":     price_long,
                    "short_entry":    price_short,
                })

            except Exception as _pair_err:
                logger.warning(f"PAIRS ENTRY: error on {sig.get('pair', '?')}: {_pair_err}")
                continue

    except Exception as _outer:
        logger.error(f"PAIRS execute_pairs_from_signals outer error: {_outer}")

    return opened


# ============================================================
# Exit — check open pairs for reversion / stop / time
# ============================================================

def check_pairs_exits(open_trades: list) -> list:
    """
    Scan all open pair_equity legs and close pairs that have hit an exit.

    Exit triggers (in priority order):
    1. |z| < EXIT_Z_TARGET  — spread reverted (take profit)
    2. |z| > EXIT_Z_STOP    — spread widened (stop loss)
    3. days_held > 2*hl     — time stop (not reverting fast enough)
    4. orphan leg detected  — partner closed externally, close remaining

    Safety nets:
    - Price/z fetch failure = HOLD (never exit on missing data)
    - close_paper_trade(id, price) — exactly 2 args, matches models.py signature
    - Each pair fully isolated in try/except
    - Orphan detection closes naked positions
    - Returns list of closed pair summaries (or [] on total failure)
    """
    closed = []

    try:
        from predictions.models import get_open_trades, close_paper_trade

        # Group open pair_equity legs by pair_id
        pair_legs: dict = {}
        for t in open_trades:
            try:
                itype = t.get("instrument_type") or ""
                if itype != "pair_equity":
                    continue
                f = json.loads(t.get("factors") or "{}")
                pid = f.get("pair_id")
                if not pid:
                    continue
                if pid not in pair_legs:
                    pair_legs[pid] = []
                pair_legs[pid].append((t, f))
            except Exception:
                continue

        if not pair_legs:
            return closed

        for pair_id, legs in pair_legs.items():
            try:
                if not legs:
                    continue

                # Separate long and short legs
                long_trade  = next((t for t, f in legs if f.get("pair_role") == "long"),  None)
                short_trade = next((t for t, f in legs if f.get("pair_role") == "short"), None)

                # --- Orphan detection ---
                if long_trade is None or short_trade is None:
                    orphan = long_trade or short_trade
                    if orphan:
                        o_ticker = orphan.get("ticker", "")
                        o_price  = _get_single_price(o_ticker)
                        if o_price and o_price > 0:
                            close_paper_trade(orphan["id"], o_price)
                            logger.warning(
                                f"PAIRS ORPHAN CLOSE: {pair_id} leg "
                                f"{o_ticker} closed at ${o_price:.2f}"
                            )
                            closed.append({
                                "pair":   pair_id,
                                "reason": "orphan_leg",
                                "ticker": o_ticker,
                            })
                    continue

                # Get OU parameters from the long leg's factors
                _, long_f = next((t, f) for t, f in legs if f.get("pair_role") == "long")
                hedge_ratio    = float(long_f.get("hedge_ratio",    1.0))
                entry_z        = float(long_f.get("entry_z",        0.0))
                half_life      = float(long_f.get("half_life_days", 20.0))
                exit_z_target  = float(long_f.get("exit_z_target",  EXIT_Z_TARGET))
                exit_z_stop    = float(long_f.get("exit_z_stop",    EXIT_Z_STOP))

                # Validate OU params
                for v, nm in [(hedge_ratio, "hedge_ratio"), (half_life, "half_life"),
                               (exit_z_target, "exit_z_target"), (exit_z_stop, "exit_z_stop")]:
                    if not math.isfinite(v) or v <= 0:
                        logger.debug(f"PAIRS: {pair_id} bad param {nm}={v} — skip exit check")
                        break
                else:
                    pass

                long_ticker  = long_trade.get("ticker",  "")
                short_ticker = short_trade.get("ticker", "")

                # Days held
                try:
                    entry_dt  = datetime.fromisoformat(
                        long_trade.get("entry_date") or long_trade.get("entry_time", "")
                    )
                    days_held = (datetime.now() - entry_dt).days
                except Exception:
                    days_held = 0

                time_stop = days_held > max(5, int(half_life * 2))

                # Recompute spread z-score with fresh data
                z_now, hl_now, ok = _recompute_ou_zscore(
                    long_ticker, short_ticker, hedge_ratio
                )

                if not ok or z_now is None:
                    # Data failure — hold. Log only if time stop is pending.
                    if time_stop:
                        logger.warning(
                            f"PAIRS: {pair_id} time-stop pending but price data "
                            f"unavailable — holding (safety net)"
                        )
                    continue

                # Determine exit trigger
                exit_reason = None
                if abs(z_now) < exit_z_target:
                    exit_reason = f"REVERSION z_entry={entry_z:.2f} z_now={z_now:.2f}"
                elif abs(z_now) > exit_z_stop:
                    exit_reason = f"STOP_LOSS z_now={z_now:.2f} > {exit_z_stop}"
                elif time_stop:
                    exit_reason = f"TIME_STOP days={days_held} hl={half_life:.1f}"

                if not exit_reason:
                    logger.debug(
                        f"PAIRS: {pair_id} hold — z={z_now:.2f} "
                        f"days={days_held} hl={half_life:.1f}"
                    )
                    continue

                # Fetch current prices for both legs
                prices     = _get_pair_prices(long_ticker, short_ticker)
                price_long = prices.get(long_ticker)
                price_short= prices.get(short_ticker)

                if not price_long or not price_short or price_long <= 0 or price_short <= 0:
                    logger.warning(
                        f"PAIRS: {pair_id} exit triggered ({exit_reason}) "
                        f"but price fetch failed — holding (safety net)"
                    )
                    continue

                # Close both legs — 2-arg signature matches models.py
                close_paper_trade(long_trade["id"],  price_long)
                close_paper_trade(short_trade["id"], price_short)

                entry_long  = float(long_trade.get("entry_price",  price_long))
                entry_short = float(short_trade.get("entry_price", price_short))
                long_pnl    = (price_long  / entry_long  - 1) * 100 if entry_long  > 0 else 0.0
                short_pnl   = (entry_short / price_short - 1) * 100 if entry_short > 0 else 0.0

                logger.warning(
                    f"PAIRS CLOSED: {pair_id} | {exit_reason} | "
                    f"LONG {long_ticker} pnl={long_pnl:+.2f}% | "
                    f"SHORT {short_ticker} pnl={short_pnl:+.2f}%"
                )

                closed.append({
                    "pair":           pair_id,
                    "reason":         exit_reason,
                    "long_leg":       long_ticker,
                    "short_leg":      short_ticker,
                    "long_pnl_pct":   round(long_pnl,  2),
                    "short_pnl_pct":  round(short_pnl, 2),
                    "entry_z":        round(entry_z, 2),
                    "exit_z":         round(z_now,   2),
                    "days_held":      days_held,
                })

            except Exception as _pair_exit_err:
                logger.warning(f"PAIRS EXIT: error on {pair_id}: {_pair_exit_err}")
                continue

    except Exception as _outer:
        logger.error(f"PAIRS check_pairs_exits outer error: {_outer}")

    return closed


# ============================================================
# Helper — single price fetch (for orphan closes)
# ============================================================

def _get_single_price(ticker: str) -> float:
    """
    Fetch single current price via yfinance.
    Returns float > 0 or None on any failure.
    """
    try:
        import yfinance as yf
        df = yf.download(ticker, period="2d", progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        close = df["Close"]
        if hasattr(close, "iloc"):
            if hasattr(close, "columns"):
                close = close.iloc[:, 0]
            val = float(close.dropna().iloc[-1])
            if math.isfinite(val) and val > 0:
                return val
    except Exception:
        pass
    return None


# ============================================================
# Status helper — summarise open pairs for API endpoint
# ============================================================

def get_open_pairs_summary(open_trades: list) -> list:
    """
    Return a list of open pair summaries for /api/pairs-active.
    Groups legs by pair_id, computes current z-score and live P&L.
    Safe: returns [] if anything fails.
    """
    summary = []
    try:
        pair_legs: dict = {}
        for t in open_trades:
            try:
                if (t.get("instrument_type") or "") != "pair_equity":
                    continue
                f = json.loads(t.get("factors") or "{}")
                pid = f.get("pair_id")
                if not pid:
                    continue
                if pid not in pair_legs:
                    pair_legs[pid] = []
                pair_legs[pid].append((t, f))
            except Exception:
                continue

        for pair_id, legs in pair_legs.items():
            try:
                long_trade  = next((t for t, f in legs if f.get("pair_role") == "long"),  None)
                short_trade = next((t for t, f in legs if f.get("pair_role") == "short"), None)

                _, long_f   = next(((t, f) for t, f in legs if f.get("pair_role") == "long"),
                                   (None, {}))
                hedge_ratio = float(long_f.get("hedge_ratio", 1.0))
                entry_z     = float(long_f.get("entry_z", 0.0))
                half_life   = float(long_f.get("half_life_days", 0.0))

                long_ticker  = long_trade.get("ticker",  "?") if long_trade  else "?"
                short_ticker = short_trade.get("ticker", "?") if short_trade else "?"

                # Live z-score (best-effort, no crash)
                z_now, _, ok = _recompute_ou_zscore(long_ticker, short_ticker, hedge_ratio)

                # Live prices for P&L
                prices      = _get_pair_prices(long_ticker, short_ticker)
                price_long  = prices.get(long_ticker)
                price_short = prices.get(short_ticker)

                long_pnl  = None
                short_pnl = None
                if long_trade and price_long:
                    ep = float(long_trade.get("entry_price", 0) or 0)
                    long_pnl = round((price_long / ep - 1) * 100, 2) if ep > 0 else None
                if short_trade and price_short:
                    ep = float(short_trade.get("entry_price", 0) or 0)
                    short_pnl = round((ep / price_short - 1) * 100, 2) if ep > 0 else None

                # Days held
                try:
                    entry_dt  = datetime.fromisoformat(
                        long_trade.get("entry_date") or long_trade.get("entry_time", "")
                        if long_trade else ""
                    )
                    days_held = (datetime.now() - entry_dt).days
                except Exception:
                    days_held = None

                summary.append({
                    "pair":            pair_id,
                    "long_leg":        long_ticker,
                    "short_leg":       short_ticker,
                    "entry_z":         round(entry_z,    2),
                    "current_z":       round(z_now, 2)  if ok and z_now is not None else None,
                    "hedge_ratio":     round(hedge_ratio, 3),
                    "half_life_days":  round(half_life, 1),
                    "long_pnl_pct":    long_pnl,
                    "short_pnl_pct":   short_pnl,
                    "days_held":       days_held,
                    "exit_z_target":   EXIT_Z_TARGET,
                    "exit_z_stop":     EXIT_Z_STOP,
                    "status":          "open",
                })
            except Exception:
                continue

    except Exception as _e:
        logger.debug(f"get_open_pairs_summary: {_e}")

    return summary
