"""
Quant Audit Fixes — implementations of the 8 priority items from the
senior-quant audit. Each function is pure, side-effect-free, and
defensive (returns sane defaults on bad input).

  #1 reconcile_long_short:   remove tickers from longs that conflict with shorts/open
  #2 adv_scaled_slippage:    Kyle's-lambda-style slippage model
  #3 isotonic_calibrate:     fit win-rate calibration from past trades
  #4 portfolio_beta_metric:  net dollar-beta exposure of current book
  #5 apply_group_penalty:    divide each factor weight by sqrt(group_size)
  #6 regime_probs:           softmax over bull/bear/sideways scores
  #7 deflated_sharpe:        Bailey-López de Prado deflated Sharpe ratio
  #8 vol_target_scaler:      scale next position by target / realized vol

All functions are safe to import without side effects. Heavy deps
(sklearn) are imported lazily inside their function so import-time
failures never crash the host module.
"""
from __future__ import annotations

import math
import logging
from typing import Iterable

logger = logging.getLogger(__name__)


# ============================================================
# #1 — Long / short reconciliation
# ============================================================

def reconcile_long_short(long_picks: list, short_picks: list,
                         open_tickers_short: Iterable[str] = (),
                         open_tickers_long: Iterable[str] = ()) -> tuple[list, list]:
    """Remove any ticker from longs that ALSO appears in shorts or in
    open-short book; remove any ticker from shorts that appears in
    open-long book. Preserves the STRONGER signal where both new picks
    conflict (whichever side has higher |composite_score|).

    Why: a self-financing dollar-neutral pair on the same ticker is
    delta≈0 but pays 2x transaction costs + spread + borrow → guaranteed
    negative EV.
    """
    if not long_picks and not short_picks:
        return long_picks or [], short_picks or []

    long_picks = list(long_picks or [])
    short_picks = list(short_picks or [])
    open_short = {str(s).upper() for s in (open_tickers_short or [])}
    open_long = {str(s).upper() for s in (open_tickers_long or [])}

    # Map by symbol
    def _sym(p):
        return str(p.get("symbol") or p.get("ticker") or "").upper()

    long_by_sym = {_sym(p): p for p in long_picks}
    short_by_sym = {_sym(p): p for p in short_picks}

    conflicted = set(long_by_sym) & set(short_by_sym)
    keep_long = set(long_by_sym)
    keep_short = set(short_by_sym)
    for sym in conflicted:
        if not sym:
            continue
        ls = abs(float(long_by_sym[sym].get("composite_score", 0) or 0))
        ss = abs(float(short_by_sym[sym].get("composite_score", 0) or 0))
        # Keep the side with stronger signal; drop the other.
        if ls >= ss:
            keep_short.discard(sym)
        else:
            keep_long.discard(sym)

    # Now also drop new picks that conflict with EXISTING open positions
    keep_long -= open_short  # don't go long something we're short
    keep_short -= open_long  # don't go short something we're long

    out_long = [p for p in long_picks if _sym(p) in keep_long]
    out_short = [p for p in short_picks if _sym(p) in keep_short]

    dropped_long = len(long_picks) - len(out_long)
    dropped_short = len(short_picks) - len(out_short)
    if dropped_long or dropped_short:
        logger.info(
            f"Long/short reconciliation: dropped {dropped_long} longs "
            f"and {dropped_short} shorts to prevent dollar-neutral conflicts"
        )
    return out_long, out_short


# ============================================================
# #2 — ADV-scaled slippage
# ============================================================

def adv_scaled_slippage_bps(order_dollars: float,
                            adv_dollars: float,
                            base_bps: float = 5.0,
                            impact_coef: float = 100.0,
                            max_bps: float = 200.0) -> float:
    """Kyle's-lambda-style sqrt impact model.

    slip_bps = base_bps + impact_coef * sqrt(order / ADV)

    impact_coef is the additional slippage (in bps) for an order equal
    to ADV (ratio=1, sqrt=1). Default 100bps per √-ratio is a typical
    institutional desk calibration.

    For order=5% of ADV, sqrt(0.05) ≈ 0.224 → adds ~22bps to base.
    For order=50% of ADV (microcap whale), sqrt(0.5) ≈ 0.71 → adds ~71bps.

    Capped at max_bps to prevent absurd numbers on near-illiquid names.
    Returns base_bps unmodified if ADV is unknown / non-positive.
    """
    try:
        adv = float(adv_dollars or 0)
        order = float(order_dollars or 0)
        if adv <= 0 or order <= 0:
            return float(base_bps)
        ratio = order / adv
        impact = impact_coef * math.sqrt(max(0.0, ratio))
        total = float(base_bps) + impact
        return min(max_bps, max(0.0, total))
    except Exception:
        return float(base_bps)


def liquidity_rejected(order_dollars: float, adv_dollars: float,
                       max_pct_adv: float = 0.05) -> bool:
    """Return True if order_dollars is too big a fraction of ADV to
    execute without material market impact. Default 5% of ADV.
    """
    try:
        if adv_dollars and adv_dollars > 0:
            return (order_dollars / adv_dollars) > max_pct_adv
    except Exception:
        pass
    return False


# ============================================================
# #3 — Isotonic calibration of confidence → P(win)
# ============================================================

def isotonic_calibrate(confidences: list, wins: list) -> "callable | None":
    """Fit an isotonic regression confidence → P(win). Returns a
    callable f(conf_array) → calibrated_probs, or None if not enough
    data / sklearn unavailable.

    Why: displayed "confidence" is a linear formula not a probability.
    Kelly sizing on uncalibrated probabilities causes geometric ruin.
    """
    try:
        c = [float(x) for x in confidences if x is not None]
        w = [1 if x else 0 for x in wins]
        if len(c) != len(w) or len(c) < 50:
            return None
        from sklearn.isotonic import IsotonicRegression  # lazy import
        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        iso.fit(c, w)

        def _apply(values):
            try:
                vs = [float(v or 0) for v in (values if hasattr(values, '__iter__') else [values])]
                return iso.predict(vs).tolist()
            except Exception:
                return [None for _ in vs]
        return _apply
    except Exception as e:
        logger.debug(f"isotonic_calibrate failed: {e}")
        return None


def expected_calibration_error(confidences: list, wins: list,
                                n_bins: int = 10) -> float:
    """ECE = Σ_bin |actual_win_rate_bin - mean_confidence_bin| × frac.
    Calibrated systems target ECE < 0.05. Returns -1 if undefined.
    """
    try:
        if not confidences or len(confidences) != len(wins) or len(confidences) < 20:
            return -1.0
        bins = [(i / n_bins, (i + 1) / n_bins) for i in range(n_bins)]
        ece = 0.0
        n = len(confidences)
        for lo, hi in bins:
            idx = [i for i, c in enumerate(confidences)
                   if lo <= (float(c or 0) / 100.0) < hi]
            if not idx:
                continue
            mean_conf = sum(float(confidences[i] or 0) / 100.0 for i in idx) / len(idx)
            mean_win = sum(1 for i in idx if wins[i]) / len(idx)
            ece += abs(mean_win - mean_conf) * (len(idx) / n)
        return round(ece, 4)
    except Exception:
        return -1.0


# ============================================================
# #4 — Portfolio dollar-beta and beta-neutrality metric
# ============================================================

def portfolio_beta_metric(positions: list) -> dict:
    """Compute net dollar-weighted beta of the open book.

    Each position contributes sign(long=+1, short=-1) * shares * price *
    beta. Total / NAV = portfolio beta to SPY. A beta-neutral book has
    |portfolio_beta| < ~0.2.
    """
    try:
        long_beta_dollars = 0.0
        short_beta_dollars = 0.0
        gross_dollars = 0.0
        for p in (positions or []):
            try:
                shares = float(p.get("shares") or 0)
                price = float(p.get("current_price") or p.get("entry_price") or 0)
                beta = float(p.get("beta") if p.get("beta") is not None
                              else p.get("actual_beta") or 1.0)
                dollars = shares * price
                gross_dollars += abs(dollars)
                if str(p.get("direction", "long")).lower() == "short":
                    short_beta_dollars += beta * abs(dollars)
                else:
                    long_beta_dollars += beta * abs(dollars)
            except Exception:
                continue
        net_beta_dollars = long_beta_dollars - short_beta_dollars
        beta = (net_beta_dollars / gross_dollars) if gross_dollars > 0 else 0.0
        return {
            "portfolio_beta": round(beta, 3),
            "long_beta_dollars": round(long_beta_dollars, 2),
            "short_beta_dollars": round(short_beta_dollars, 2),
            "net_beta_dollars": round(net_beta_dollars, 2),
            "beta_neutral": abs(beta) < 0.2,
        }
    except Exception as e:
        logger.debug(f"portfolio_beta_metric failed: {e}")
        return {"portfolio_beta": 0, "beta_neutral": True}


# ============================================================
# #5 — Factor group penalty for collinearity
# ============================================================

FACTOR_GROUPS = {
    "trend":         ["momentum", "relative_strength", "ichimoku", "mtf_alignment", "vwap"],
    "mean_reversion": ["rsi2", "bb_squeeze", "vol_compression"],
    "volatility":    ["low_vol", "kurtosis", "hurst", "autocorr"],
    "volume":        ["volume", "smart_money", "vpoc"],
    "fundamental":   ["value", "quality", "earnings_drift"],
    "microstructure": ["stat_arb", "candlestick", "sector_rotation", "beta", "fundamental_value"],
}


def apply_group_penalty(factor_weights: dict) -> dict:
    """Divide each factor's weight by sqrt(group_size) to dampen the
    common-component amplification when factors in the same group are
    correlated. Returns a new dict with weights re-normalized to sum=1.

    Math: when factors in a group share correlation ρ, naive equal
    weights amplify the common signal by ~√(1+(k-1)ρ). Dividing by
    √k partially compensates without requiring the correlation matrix.
    """
    if not factor_weights:
        return factor_weights or {}
    try:
        # Build lookup: factor → group_size
        factor_to_group_size = {}
        for grp, members in FACTOR_GROUPS.items():
            for f in members:
                factor_to_group_size[f] = len(members)
        scaled = {}
        for f, w in factor_weights.items():
            try:
                wval = float(w or 0)
                k = factor_to_group_size.get(f, 1)
                scaled[f] = wval / math.sqrt(max(1, k))
            except (TypeError, ValueError):
                scaled[f] = float(w or 0)
        total = sum(scaled.values())
        if total > 0:
            return {f: round(v / total, 6) for f, v in scaled.items()}
        return factor_weights
    except Exception as e:
        logger.debug(f"apply_group_penalty failed: {e}")
        return factor_weights


# ============================================================
# #6 — Soft regime probabilities via softmax
# ============================================================

def regime_probs(bull_score: float, bear_score: float,
                 sideways_score: float | None = None,
                 temperature: float = 1.0) -> dict:
    """Convert hand-tuned bull/bear integer scores into a probability
    distribution over {BULL, BEAR, SIDEWAYS} via tempered softmax.

    temperature=1.0 standard softmax; higher = smoother. Used as
    mixing weights so transitions across regime boundaries are
    continuous rather than discrete (no portfolio churn at threshold).
    """
    try:
        bull = float(bull_score or 0)
        bear = float(bear_score or 0)
        # Implicit "sideways score" = 1 when bull and bear are both low
        side = float(sideways_score) if sideways_score is not None else max(0.0, 2.0 - max(bull, bear))
        t = max(0.1, float(temperature))
        scores = [bull / t, bear / t, side / t]
        m = max(scores)
        exps = [math.exp(s - m) for s in scores]
        z = sum(exps)
        if z <= 0:
            return {"BULL": 0.33, "BEAR": 0.33, "SIDEWAYS": 0.34}
        return {
            "BULL":     round(exps[0] / z, 4),
            "BEAR":     round(exps[1] / z, 4),
            "SIDEWAYS": round(exps[2] / z, 4),
        }
    except Exception:
        return {"BULL": 0.33, "BEAR": 0.33, "SIDEWAYS": 0.34}


# ============================================================
# #7 — Deflated Sharpe Ratio (Bailey & López de Prado 2014)
# ============================================================

def deflated_sharpe(sr: float, n_obs: int, n_trials: int,
                    skew: float = 0.0, kurt: float = 3.0) -> dict:
    """Deflated Sharpe — adjusts a measured Sharpe down to account for
    (a) finite-sample variance, (b) non-normal returns (skew/kurt),
    (c) multiple-hypothesis selection across n_trials.

    DSR ≈ Φ( (SR - SR_expected_max) * √(N-1) / σ_SR )

    Where σ_SR² = (1 - skew*SR + (kurt-1)/4 * SR²) / (N-1)
    and SR_expected_max is the expected max Sharpe under the null over
    n_trials, approximated by Z(1 - 1/N) * √(2 ln(n_trials))

    Returns dict with raw SR, deflation, probability that true SR > 0.
    A backtest SR of 1.5 with 50 trials and 200 obs often deflates to
    near zero — a critical guard against overfit.
    """
    try:
        sr = float(sr)
        n = max(2, int(n_obs))
        trials = max(1, int(n_trials))
        sk = float(skew or 0)
        ku = float(kurt or 3)
        # Stdev of SR estimator under non-normal returns
        var_sr = (1.0 - sk * sr + ((ku - 1.0) / 4.0) * (sr ** 2)) / (n - 1)
        sd_sr = math.sqrt(max(1e-9, var_sr))
        # Expected maximum SR under the null (Bonferroni-style)
        # Approximation: Z * sqrt(2*log(trials)), where Z is std-normal quantile
        # Simpler approximation: 0.577 (Euler-Mascheroni) + log term
        sr_max_null = math.sqrt(2.0 * math.log(max(2, trials))) * (
            (1.0 - 0.5772 / max(2, trials)) ** 0.5
        )
        # Probability the observed SR > expected max under null
        # Φ(z) where z = (sr - sr_max_null) / sd_sr
        z = (sr - sr_max_null) / sd_sr
        # Approx standard-normal CDF
        prob = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        return {
            "raw_sharpe": round(sr, 3),
            "n_obs": n,
            "n_trials": trials,
            "expected_max_null_sharpe": round(sr_max_null, 3),
            "sharpe_stderr": round(sd_sr, 3),
            "deflated_sharpe_zscore": round(z, 3),
            "prob_skill": round(prob, 4),
            "verdict": ("skill" if prob > 0.95 else
                        "marginal" if prob > 0.75 else
                        "likely_overfit"),
        }
    except Exception as e:
        return {"raw_sharpe": sr, "error": str(e)[:120], "prob_skill": None}


# ============================================================
# #8 — Vol targeting scaler
# ============================================================

def vol_target_scaler(realized_vol_annualized: float,
                      target_vol_annualized: float = 0.12,
                      max_scale: float = 1.5,
                      min_scale: float = 0.3) -> float:
    """Returns multiplier for the next position size to keep portfolio
    realized vol near target_vol_annualized (default 12%).

    scale = target / realized   (clamped to [min_scale, max_scale])

    During calm markets (realized=8%, target=12%): scale=1.5 → upsize.
    During panic (realized=30%, target=12%): scale=0.4 → downsize.

    Why: Sharpe-stabilizing. Without this, a vol regime change can
    halve or double effective leverage without you knowing.
    """
    try:
        rv = float(realized_vol_annualized or 0)
        tv = float(target_vol_annualized or 0.12)
        if rv <= 1e-6 or tv <= 1e-6:
            return 1.0
        raw = tv / rv
        return float(max(min_scale, min(max_scale, raw)))
    except Exception:
        return 1.0


def estimate_realized_vol_from_trades(closed_trades: list,
                                      trades_per_year: int = 252) -> float:
    """Annualized stdev of trade pnl_pct. Used as the input to
    vol_target_scaler. Returns 0 if not enough data."""
    try:
        rets = [float(t.get("pnl_pct") or 0) / 100.0
                for t in (closed_trades or [])
                if t.get("pnl_pct") is not None]
        if len(rets) < 10:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
        sd = math.sqrt(var)
        return sd * math.sqrt(max(1, trades_per_year))
    except Exception:
        return 0.0
