"""
Regression suite for the source-boundary data validator (2026-08-07).

Covers validate_ohlcv() and the trusted-price gating in
get_historical_any_source(). Standalone (no pytest):

    cd backend && python3 test_source_validation.py

The validator is the single gate that keeps a grossly-wrong historical series
(e.g. V at $3.30 vs a live $362.50, a frozen frame, or a split-glitched last
bar) from ever reaching the scoring engine. It must NEVER reject a legitimate
series and NEVER accept a corrupt one.
"""
import pandas as pd
import numpy as np
import analytics.multi_source_adapter as msa
from analytics.multi_source_adapter import validate_ohlcv, get_historical_any_source

_checks = []


def check(name, cond):
    _checks.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


def mkdf(closes):
    idx = pd.date_range("2025-01-01", periods=len(closes), freq="D")
    closes = list(closes)
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes,
         "Volume": [1e6] * len(closes)},
        index=idx,
    )


_none = lambda *a, **k: None


def _restore_all(frame):
    """Point every underlying source at a single frame (or callable)."""
    fn = frame if callable(frame) else (lambda t, p: frame)
    msa.get_tiingo_historical = fn
    msa.get_alphavantage_historical = _none
    msa.get_fmp_historical = _none
    msa.get_twelvedata_historical = _none
    msa.get_polygon_historical = _none


def run():
    rng = np.random.default_rng(1)
    good = mkdf(list(360.0 + np.cumsum(rng.normal(0, 1, 120))))
    good_last = float(good["Close"].values[-1])

    # ---- validate_ohlcv structural ----
    check("1 good structural accepted", validate_ohlcv(good) is True)
    check("2 good vs matching trusted accepted",
          validate_ohlcv(good, trusted_px=good_last) is True)
    check("3 good vs trusted 5% off accepted",
          validate_ohlcv(good, trusted_px=good_last * 1.05) is True)
    check("4 good vs trusted 40% off rejected",
          validate_ohlcv(good, trusted_px=good_last * 0.60) is False)
    check("5 frozen series rejected", validate_ohlcv(mkdf([3.30] * 120)) is False)
    glitch = mkdf(list(360.0 + np.cumsum(np.random.default_rng(2).normal(0, 1, 119))) + [3.30])
    check("6 outlier last-bar rejected", validate_ohlcv(glitch) is False)
    check("7 zero close rejected",
          validate_ohlcv(mkdf([360.0] * 60 + [0.0] + [360.0] * 59)) is False)
    check("8 negative close rejected",
          validate_ohlcv(mkdf([360.0] * 60 + [-5.0] + [360.0] * 59)) is False)
    check("9 fewer than 5 rows rejected", validate_ohlcv(mkdf([100, 101, 102])) is False)
    check("10 None rejected", validate_ohlcv(None) is False)
    check("11 empty frame rejected", validate_ohlcv(pd.DataFrame()) is False)
    check("12 no Close column rejected",
          validate_ohlcv(pd.DataFrame({"Open": [1, 2, 3, 4, 5]})) is False)
    trend = mkdf(list(np.linspace(300, 400, 120)))
    check("13 legit uptrend accepted (33% over 120 bars)", validate_ohlcv(trend) is True)
    check("14 legit uptrend vs its trusted last accepted",
          validate_ohlcv(trend, trusted_px=float(trend["Close"].values[-1])) is True)
    downt = mkdf(list(np.linspace(400, 300, 120)))
    check("15 legit downtrend accepted", validate_ohlcv(downt) is True)
    # NaN close
    nanser = list(360.0 + np.cumsum(rng.normal(0, 1, 120)))
    nan_df = mkdf(nanser)
    nan_df.iloc[-1, nan_df.columns.get_loc("Close")] = float("nan")
    # last real close is second-to-last after dropna -> still valid structural
    check("16 trailing NaN handled (drops to prior close)",
          validate_ohlcv(nan_df) is True)

    # ---- get_historical_any_source selection ----
    bad = mkdf(list(3.0 + np.cumsum(np.random.default_rng(9).normal(0, 0.02, 120))))

    # A: corrupt first source skipped for a matching later source
    msa.get_tiingo_historical = lambda t, p: bad
    msa.get_alphavantage_historical = _none
    msa.get_fmp_historical = lambda t, p: good
    msa.get_twelvedata_historical = _none
    msa.get_polygon_historical = _none
    r = get_historical_any_source("V", "1y", trusted_px=good_last)
    check("17 corrupt source skipped, matching source chosen",
          r is not None and abs(float(r["Close"].values[-1]) - good_last) < 1e-6)

    # B: all sources disagree with trusted -> None (withhold)
    _restore_all(lambda t, p: bad)
    r = get_historical_any_source("V", "1y", trusted_px=good_last)
    check("18 all-corrupt + trusted quote -> None", r is None)

    # C: no trusted quote -> frozen skipped, good structural returned
    msa.get_tiingo_historical = lambda t, p: mkdf([3.30] * 120)  # frozen
    msa.get_alphavantage_historical = _none
    msa.get_fmp_historical = lambda t, p: good
    msa.get_twelvedata_historical = _none
    msa.get_polygon_historical = _none
    r = get_historical_any_source("V", "1y", trusted_px=None)
    check("19 no trusted: frozen skipped, structural frame returned",
          r is not None and abs(float(r["Close"].values[-1]) - good_last) < 1e-6)

    # D: backward-compatible 2-arg call still works
    _restore_all(lambda t, p: good)
    check("20 backward-compatible 2-arg call", get_historical_any_source("V", "1y") is not None)

    # E: good first source returned immediately
    _restore_all(lambda t, p: good)
    r = get_historical_any_source("V", "1y", trusted_px=good_last)
    check("21 good first source returned", r is not None)

    # F: no trusted + every source corrupt (all frozen) -> None
    _restore_all(lambda t, p: mkdf([3.30] * 120))
    check("22 no trusted, all frozen -> None",
          get_historical_any_source("V", "1y", trusted_px=None) is None)

    # G: source raising an exception is skipped, next good source used
    def _boom(t, p):
        raise RuntimeError("source down")
    msa.get_tiingo_historical = _boom
    msa.get_alphavantage_historical = lambda t, p: good
    msa.get_fmp_historical = _none
    msa.get_twelvedata_historical = _none
    msa.get_polygon_historical = _none
    r = get_historical_any_source("V", "1y", trusted_px=good_last)
    check("23 exploding source skipped, next good source used", r is not None)

    # ---- BODY-based trusted check (the JPM regression) ----
    # A frame with a coincidentally-correct last close but a wrong BODY must be
    # rejected under a tight tolerance so the next source is tried, while a frame
    # with a correct body but a single bad final print is ACCEPTED (the last bar
    # is anchored downstream).
    jpm_bad = mkdf([316.0] * 119 + [357.0])  # body 316 vs live 357 (~11.5% off)
    check("24 JPM-style bad body accepted at loose 30% tol",
          validate_ohlcv(jpm_bad, trusted_px=357.0, tol=0.30) is True)
    check("25 JPM-style bad body rejected at tight 8% tol",
          validate_ohlcv(jpm_bad, trusted_px=357.0, tol=0.08) is False)
    good_body_bad_tail = mkdf(
        list(357.0 + np.cumsum(np.random.default_rng(7).normal(0, 0.5, 119))) + [500.0])
    check("26 good body + bad final print accepted (tail anchored downstream)",
          validate_ohlcv(good_body_bad_tail, trusted_px=357.0, tol=0.08) is True)

    # H: with a tight tol, the bad-body source is skipped and the body-
    # corroborating source is selected (JPM would score instead of withhold).
    msa.get_tiingo_historical = lambda t, p: jpm_bad          # right last close, wrong body
    msa.get_alphavantage_historical = lambda t, p: mkdf([357.0] * 120)  # frozen -> rejected
    msa.get_fmp_historical = lambda t, p: mkdf(
        list(357.0 + np.cumsum(np.random.default_rng(2).normal(0, 0.5, 120))))  # good body
    msa.get_twelvedata_historical = _none
    msa.get_polygon_historical = _none
    r = get_historical_any_source("JPM", "1y", trusted_px=357.0, tol=0.08)
    body = float(np.median(r["Close"].values[-6:-1])) if r is not None else None
    check("27 tight tol: bad-body source skipped, body-corroborating source chosen",
          r is not None and abs(body - 357.0) / 357.0 <= 0.08)

    # I: tight tol + every source body-off -> None (genuinely ambiguous -> withhold)
    _restore_all(lambda t, p: jpm_bad)
    check("28 tight tol, all bodies off -> None",
          get_historical_any_source("JPM", "1y", trusted_px=357.0, tol=0.08) is None)

    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{passed}/{total} checks passed")
    if passed != total:
        raise SystemExit(1)


if __name__ == "__main__":
    run()
