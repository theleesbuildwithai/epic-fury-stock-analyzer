"""
Position Correlation Matrix — ADVISORY ONLY.

Real-time check: are the open positions actually diversified, or are
they all the same bet wearing different tickers?

CRITICAL SAFETY:
  This module NEVER blocks trades, NEVER pauses cycles, NEVER
  prevents pick generation. It is READ-ONLY. It exposes warnings
  via API endpoints. The user (or the user's pick logic) decides
  whether to act on the warnings.

CORE FUNCTIONS:
  1. get_open_positions_correlation() — pulls last 60d of price data
     for all open positions, computes pairwise correlation matrix
  2. get_concentration_warnings() — flags issues:
       - Max sector weight > 40% (sector concentration)
       - Average pairwise correlation > 0.7 (high cluster)
       - Any pair > 0.95 (effectively duplicate bet)
  3. get_diversification_score() — single number 0-100, higher is
     more diversified

OUTPUT:
  Plain dictionaries. No side effects. No DB writes.
"""

import logging
import time
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

CORRELATION_LOOKBACK_DAYS = 60
HIGH_CORR_THRESHOLD = 0.70
DUPLICATE_CORR_THRESHOLD = 0.95
SECTOR_CONCENTRATION_THRESHOLD = 0.40

_cache = {"data": None, "ts": 0}
_CACHE_TTL = 600    # 10 minutes
_lock = threading.Lock()


def _safe_recent_closes(tickers: list, days: int) -> dict:
    """Pull last `days` of closes for each ticker. Returns
    {ticker: [list of closes]} dict. Soft-fails per ticker."""
    out = {}
    try:
        import yfinance as yf
        import pandas as pd
        end = datetime.utcnow()
        start = end - timedelta(days=int(days) + 5)
        df = yf.download(tickers, start=start.strftime("%Y-%m-%d"),
                         end=end.strftime("%Y-%m-%d"),
                         progress=False, auto_adjust=True, threads=True,
                         group_by="ticker")
        if df is None or df.empty:
            return out
        for t in tickers:
            try:
                if isinstance(df.columns, pd.MultiIndex):
                    if t in df.columns.get_level_values(0):
                        s = df[(t, "Close")].dropna().values.tolist()
                        if len(s) >= 30:
                            out[t] = [float(x) for x in s]
                elif len(tickers) == 1:
                    s = df["Close"].dropna().values.tolist()
                    if len(s) >= 30:
                        out[t] = [float(x) for x in s]
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"_safe_recent_closes soft-fail: {e}")
    return out


def _pearson(a: list, b: list) -> float:
    """Pearson correlation. Returns 0 on degenerate input. Never raises."""
    try:
        n = min(len(a), len(b))
        if n < 5:
            return 0.0
        a, b = a[-n:], b[-n:]
        mean_a = sum(a) / n
        mean_b = sum(b) / n
        var_a = sum((x - mean_a) ** 2 for x in a) / n
        var_b = sum((x - mean_b) ** 2 for x in b) / n
        if var_a == 0 or var_b == 0:
            return 0.0
        cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n)) / n
        return round(cov / ((var_a * var_b) ** 0.5), 3)
    except Exception:
        return 0.0


def _to_returns(closes: list) -> list:
    """Convert close prices to daily log returns. Returns [] on bad input."""
    try:
        if len(closes) < 2:
            return []
        out = []
        for i in range(1, len(closes)):
            if closes[i-1] > 0 and closes[i] > 0:
                out.append(closes[i] / closes[i-1] - 1)
        return out
    except Exception:
        return []


def get_open_positions_correlation(force_refresh: bool = False) -> dict:
    """Pulls open positions, computes pairwise correlation matrix on
    daily returns. Soft-fails to {ok: False}. Cached 10 min."""
    try:
        with _lock:
            if (not force_refresh and _cache.get("data")
                    and (time.time() - _cache.get("ts", 0)) < _CACHE_TTL):
                cached = dict(_cache["data"])
                cached["_cached"] = True
                cached["_cache_age_seconds"] = int(time.time() - _cache["ts"])
                return cached

        from predictions.models import get_open_trades
        positions = get_open_trades() or []
        # Equity-only correlation (skip options — different price dynamics)
        equity_positions = [p for p in positions
                            if (p.get("instrument_type") or "equity") == "equity"]
        if len(equity_positions) < 2:
            return {"ok": True, "n_positions": len(equity_positions),
                    "message": "need >=2 equity positions for correlation",
                    "matrix": {}, "warnings": []}

        tickers = sorted(set(p["ticker"] for p in equity_positions))
        closes_by_ticker = _safe_recent_closes(tickers, CORRELATION_LOOKBACK_DAYS)
        if len(closes_by_ticker) < 2:
            return {"ok": False,
                    "reason": f"only_{len(closes_by_ticker)}_tickers_with_data"}

        # Compute pairwise correlations on RETURNS (not prices)
        returns_by_ticker = {t: _to_returns(c) for t, c in closes_by_ticker.items()}
        matrix = {}
        all_pairs = []
        ticker_list = sorted(returns_by_ticker.keys())
        for i, t1 in enumerate(ticker_list):
            matrix[t1] = {}
            for j, t2 in enumerate(ticker_list):
                if i == j:
                    matrix[t1][t2] = 1.0
                elif j < i and t2 in matrix and t1 in matrix[t2]:
                    matrix[t1][t2] = matrix[t2][t1]   # symmetric
                else:
                    c = _pearson(returns_by_ticker[t1], returns_by_ticker[t2])
                    matrix[t1][t2] = c
                    if i < j:
                        all_pairs.append({"t1": t1, "t2": t2, "corr": c})

        # Diversification metrics
        if all_pairs:
            corrs = [p["corr"] for p in all_pairs]
            avg_corr = sum(corrs) / len(corrs)
            max_pair = max(all_pairs, key=lambda p: p["corr"])
        else:
            avg_corr = 0
            max_pair = None

        # Sector concentration
        from collections import Counter
        sector_counts = Counter(p.get("sector") or "Unknown"
                                 for p in equity_positions)
        total_pos = len(equity_positions)
        sector_weights = {s: round(c / total_pos, 3)
                          for s, c in sector_counts.items()}
        max_sector = max(sector_weights.items(), key=lambda x: x[1])

        # Diversification score 0-100
        # Components: low avg corr (40), low max sector (40), # tickers (20)
        score = 0
        score += max(0, (1 - avg_corr) * 40)
        score += max(0, (1 - max_sector[1]) * 40)
        score += min(20, len(ticker_list))

        result = {
            "ok": True,
            "computed_at": datetime.utcnow().isoformat(),
            "_cached": False,
            "n_positions": total_pos,
            "n_unique_tickers": len(ticker_list),
            "tickers": ticker_list,
            "avg_pairwise_correlation": round(avg_corr, 3),
            "max_pair": max_pair,
            "sector_weights": sector_weights,
            "max_sector": {"sector": max_sector[0], "weight": max_sector[1]},
            "diversification_score": round(score, 1),
            "matrix": matrix,
            "warnings": _build_warnings(avg_corr, max_pair, max_sector,
                                         all_pairs),
        }
        with _lock:
            _cache["data"] = result
            _cache["ts"] = time.time()
        return result
    except Exception as e:
        logger.warning(f"get_open_positions_correlation fail: {e}")
        return {"ok": False, "reason": str(e)[:200]}


def _build_warnings(avg_corr: float, max_pair: dict, max_sector: tuple,
                     all_pairs: list) -> list:
    """Pure: build advisory warnings. Returns list of {severity, message}."""
    warnings = []
    if avg_corr >= HIGH_CORR_THRESHOLD:
        warnings.append({
            "severity": "high",
            "type": "high_avg_correlation",
            "message": (f"Average pairwise correlation {avg_corr:.2f} >= "
                        f"{HIGH_CORR_THRESHOLD} — positions are clustered, "
                        f"diversification is illusory"),
        })
    if max_pair and max_pair["corr"] >= DUPLICATE_CORR_THRESHOLD:
        warnings.append({
            "severity": "medium",
            "type": "duplicate_bet",
            "message": (f"{max_pair['t1']} and {max_pair['t2']} correlate "
                        f"at {max_pair['corr']:.3f} — effectively the same trade"),
        })
    if max_sector and max_sector[1] >= SECTOR_CONCENTRATION_THRESHOLD:
        warnings.append({
            "severity": "medium",
            "type": "sector_concentration",
            "message": (f"{max_sector[1]*100:.0f}% of positions in "
                        f"{max_sector[0]} (over {SECTOR_CONCENTRATION_THRESHOLD*100:.0f}% threshold)"),
        })
    # Detect 3+ highly-correlated cluster
    if all_pairs:
        high_corr_pairs = [p for p in all_pairs if p["corr"] >= HIGH_CORR_THRESHOLD]
        if len(high_corr_pairs) >= 3:
            tickers_in_cluster = set()
            for p in high_corr_pairs:
                tickers_in_cluster.add(p["t1"])
                tickers_in_cluster.add(p["t2"])
            warnings.append({
                "severity": "medium",
                "type": "correlated_cluster",
                "message": (f"{len(tickers_in_cluster)} tickers form "
                            f"high-corr cluster: {sorted(tickers_in_cluster)}"),
            })
    return warnings


def get_concentration_summary() -> dict:
    """Lightweight summary endpoint — just warnings + score, no full matrix."""
    try:
        full = get_open_positions_correlation()
        if not full.get("ok"):
            return full
        # Strip the heavy matrix
        out = {k: v for k, v in full.items() if k != "matrix"}
        out["matrix_omitted"] = True
        return out
    except Exception as e:
        return {"ok": False, "reason": str(e)[:200]}
