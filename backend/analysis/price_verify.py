"""
Cross-Source Price Verification — a second, independent opinion on the one
number every verdict depends on.

The analyze path prices a stock from Yahoo-derived data. Before the decision
engine grades it, we ask CNBC (approved source, different infrastructure) for
the same quote and measure the disagreement:

  deviation < 3%   -> VERIFIED   (sources agree; normal intraday noise)
  3% - 10%         -> TOLERANCE  (sources differ; verdict stands, conviction cut)
  deviation > 10%  -> MISMATCH   (one source is wrong; verdict is WITHHELD)
  no reference     -> UNVERIFIED (CNBC unreachable; proceed, labeled)

FAIL-SAFE: verification infrastructure being down must never block analysis —
only an actual measured mismatch can. Thread + hard timeout so a hung CNBC
call can never stall the analyze request. Zero Yahoo calls by design.
"""

import threading

VERIFY_WARN_PCT = 3.0
VERIFY_MISMATCH_PCT = 10.0
_TIMEOUT_S = 6


def classify_deviation(live_price, reference_price):
    """Pure classification of two prices. Never raises.

    Returns {status, deviation_pct, reference_price} with status one of
    VERIFIED / TOLERANCE / MISMATCH / UNVERIFIED.
    """
    try:
        lp = float(live_price)
        rp = float(reference_price)
        if lp != lp or rp != rp or lp <= 0 or rp <= 0:
            return {"status": "UNVERIFIED", "deviation_pct": None,
                    "reference_price": None}
        dev = abs(lp - rp) / lp * 100.0
        if dev > VERIFY_MISMATCH_PCT:
            status = "MISMATCH"
        elif dev >= VERIFY_WARN_PCT:
            status = "TOLERANCE"
        else:
            status = "VERIFIED"
        return {"status": status, "deviation_pct": round(dev, 2),
                "reference_price": round(rp, 2)}
    except (TypeError, ValueError):
        return {"status": "UNVERIFIED", "deviation_pct": None,
                "reference_price": None}


def verify_price(ticker, live_price):
    """Fetch a CNBC reference quote (timeout-guarded) and classify.

    Returns {status, deviation_pct, reference_price, source}. Never raises;
    worst case is UNVERIFIED.
    """
    out = {"status": "UNVERIFIED", "deviation_pct": None,
           "reference_price": None, "source": "cnbc"}
    try:
        lp = float(live_price)
        if lp != lp or lp <= 0:
            return out
    except (TypeError, ValueError):
        return out

    box = [None]

    def _pull(_t=str(ticker).upper(), _b=box):
        try:
            from analysis.extras import cnbc_quote_batch
            q = cnbc_quote_batch([_t]) or {}
            _b[0] = (q.get(_t) or {}).get("price")
        except Exception:
            _b[0] = None

    try:
        th = threading.Thread(target=_pull, daemon=True)
        th.start()
        th.join(timeout=_TIMEOUT_S)
    except Exception:
        pass

    ref = box[0]
    try:
        from analytics.source_health import record
        record("cnbc_price_verify", ok=ref is not None)
    except Exception:
        pass

    result = classify_deviation(live_price, ref)
    result["source"] = "cnbc"
    return result
