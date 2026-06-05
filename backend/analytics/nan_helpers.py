"""
Shared NaN/Inf safety. Every analytics function should pass values
through these before returning to JSON responses.
"""
import math
from typing import Any


def scrub_nan(obj: Any) -> Any:
    """Recursively replace NaN/Inf floats with None (JSON-safe)."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: scrub_nan(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub_nan(v) for v in obj]
    return obj


def safe_float(x, default=None):
    """Convert to float, return default if NaN/Inf/None/error."""
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def safe_div(num, den, default=0.0):
    """Division that never raises."""
    try:
        if den == 0 or den is None:
            return default
        result = num / den
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (TypeError, ZeroDivisionError):
        return default


def clamp(x, lo, hi):
    """Clamp x to [lo, hi]. Returns lo if x is None or NaN."""
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return lo
    return max(lo, min(hi, x))


def percentile(values: list, p: float) -> float:
    """p-th percentile of values. p in [0, 100]. Returns None if empty."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    if n == 1:
        return s[0]
    k = (n - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, n - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)
