"""
Strategy Filters — pluggable quality gates for trade picks.

Provides additional safety/quality filters that can be applied to picks
before execution, to reduce whipsaw and improve win rate without changing
the core selection logic.

Each filter takes a pick + context and returns (allow: bool, reason: str).
"""

import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta
from threading import Lock

logger = logging.getLogger(__name__)

# ─── 1. SECTOR COOLDOWN (anti-whipsaw) ────────────────────────────────────────
# If a sector flipped direction within the last N hours, require higher
# confidence to enter a new trade in that sector. Reduces death-spiral
# whipsaws where the system flips back-and-forth on noise.

_sector_direction_history = defaultdict(deque)  # sector -> deque of (timestamp, direction)
_sector_lock = Lock()
SECTOR_COOLDOWN_HOURS = 6
SECTOR_FLIP_PENALTY = 15  # confidence reduction for trading a recently-flipped sector


def record_sector_trade(sector: str, direction: str):
    """Log a trade in a sector. Used by the cooldown filter."""
    if not sector:
        return
    with _sector_lock:
        history = _sector_direction_history[sector]
        history.append((datetime.now(), direction))
        # Keep only last 24h of history
        cutoff = datetime.now() - timedelta(hours=24)
        while history and history[0][0] < cutoff:
            history.popleft()


def check_sector_cooldown(sector: str, direction: str, confidence: int) -> tuple:
    """
    Check if this sector has flipped direction recently.
    Returns (allow: bool, adjusted_confidence: int, reason: str).

    Logic:
    - If sector traded the OPPOSITE direction in last SECTOR_COOLDOWN_HOURS,
      reduce confidence by SECTOR_FLIP_PENALTY
    - If confidence drops below 40 after penalty, block the trade
    """
    if not sector or not direction:
        return True, confidence, "no sector/direction"

    cutoff = datetime.now() - timedelta(hours=SECTOR_COOLDOWN_HOURS)
    with _sector_lock:
        history = list(_sector_direction_history.get(sector, []))

    recent_opposite = sum(
        1 for ts, d in history
        if ts >= cutoff and d != direction and d in ("long", "short")
    )

    if recent_opposite == 0:
        return True, confidence, "no recent flips"

    adjusted = confidence - (SECTOR_FLIP_PENALTY * min(recent_opposite, 2))
    if adjusted < 40:
        return False, adjusted, (
            f"SECTOR COOLDOWN: {sector} flipped {recent_opposite}x in last "
            f"{SECTOR_COOLDOWN_HOURS}h, conf {confidence}→{adjusted} below floor"
        )
    return True, adjusted, (
        f"SECTOR COOLDOWN: {sector} recently flipped, conf {confidence}→{adjusted}"
    )


# ─── 2. RAPID-FIRE PROTECTION (anti-overtrading) ──────────────────────────────
# If we already opened > N trades in the last 60 min, slow down — this catches
# situations where one signal source is going crazy and spamming picks.

_recent_opens = deque()  # (timestamp, symbol) tuples
_opens_lock = Lock()
RAPID_FIRE_WINDOW_MIN = 60
RAPID_FIRE_MAX_OPENS = 8


def record_open(symbol: str):
    """Log a trade open. Used by rapid-fire protection."""
    if not symbol:
        return
    with _opens_lock:
        _recent_opens.append((datetime.now(), symbol))
        cutoff = datetime.now() - timedelta(minutes=RAPID_FIRE_WINDOW_MIN)
        while _recent_opens and _recent_opens[0][0] < cutoff:
            _recent_opens.popleft()


def check_rapid_fire() -> tuple:
    """
    Returns (allow: bool, reason: str).
    Blocks if we've already opened more than RAPID_FIRE_MAX_OPENS in window.
    """
    cutoff = datetime.now() - timedelta(minutes=RAPID_FIRE_WINDOW_MIN)
    with _opens_lock:
        recent = sum(1 for ts, _ in _recent_opens if ts >= cutoff)
    if recent >= RAPID_FIRE_MAX_OPENS:
        return False, (
            f"RAPID FIRE: {recent} opens in last {RAPID_FIRE_WINDOW_MIN}min, "
            f"limit {RAPID_FIRE_MAX_OPENS} — slowing down"
        )
    return True, f"OK ({recent}/{RAPID_FIRE_MAX_OPENS} opens this hour)"


# ─── 3. OPTIONS TIMING FILTER ─────────────────────────────────────────────────
# Skip new options trades during the first 15 min and last 15 min of trading.
# Wide spreads at these times = bad fills. Equity is fine; only options gated.

import pytz
ET_TZ = pytz.timezone("US/Eastern")


def options_timing_ok() -> tuple:
    """
    Returns (allow: bool, reason: str).
    Blocks options trades during opening volatility (9:30-9:45) and closing
    auction (3:45-4:00) when spreads widen and fills are bad.
    """
    now_et = datetime.now(ET_TZ)
    if now_et.weekday() >= 5:
        return False, "weekend"
    h, m = now_et.hour, now_et.minute
    minutes_from_open = (h - 9) * 60 + (m - 30) if h >= 9 else -1
    minutes_to_close = (16 - h) * 60 - m

    if 0 <= minutes_from_open < 15:
        return False, f"OPTIONS TIMING: opening 15min — wide spreads, skip"
    if 0 < minutes_to_close <= 15:
        return False, f"OPTIONS TIMING: closing 15min — auction risk, skip"
    if minutes_to_close <= 0 or minutes_from_open < 0:
        return False, f"OPTIONS TIMING: outside RTH"
    return True, f"OK ({minutes_from_open}min from open, {minutes_to_close}min to close)"


# ─── 4. CONFIDENCE FLOOR (sanity check) ──────────────────────────────────────
# If the system is generating picks with confidence < 30, something is wrong
# with the signal — don't trade noise.

CONFIDENCE_FLOOR = 30


def check_confidence_floor(confidence: int) -> tuple:
    """Returns (allow, reason). Blocks picks below the noise threshold."""
    if confidence is None:
        return False, "CONFIDENCE: missing — block"
    if confidence < CONFIDENCE_FLOOR:
        return False, f"CONFIDENCE FLOOR: {confidence} < {CONFIDENCE_FLOOR} — likely noise"
    return True, f"OK (conf {confidence})"


# ─── 5. COMBINED FILTER (apply all in order) ──────────────────────────────────

def apply_all_filters(pick: dict) -> dict:
    """
    Apply all filters to a pick. Returns:
      {allow: bool, adjusted_confidence: int, reasons: [str], blocking_filter: str}
    """
    sector = pick.get("sector", "")
    direction = pick.get("direction", "long")
    confidence = pick.get("confidence", 0)
    instrument = pick.get("instrument_type", "equity")

    reasons = []
    final_confidence = confidence

    # 1. Confidence floor
    ok, reason = check_confidence_floor(confidence)
    reasons.append(reason)
    if not ok:
        return {"allow": False, "adjusted_confidence": confidence,
                "reasons": reasons, "blocking_filter": "confidence_floor"}

    # 2. Sector cooldown (adjusts confidence)
    ok, final_confidence, reason = check_sector_cooldown(sector, direction, confidence)
    reasons.append(reason)
    if not ok:
        return {"allow": False, "adjusted_confidence": final_confidence,
                "reasons": reasons, "blocking_filter": "sector_cooldown"}

    # 3. Rapid fire
    ok, reason = check_rapid_fire()
    reasons.append(reason)
    if not ok:
        return {"allow": False, "adjusted_confidence": final_confidence,
                "reasons": reasons, "blocking_filter": "rapid_fire"}

    # 4. Options timing (only for options)
    if instrument in ("call", "put"):
        ok, reason = options_timing_ok()
        reasons.append(reason)
        if not ok:
            return {"allow": False, "adjusted_confidence": final_confidence,
                    "reasons": reasons, "blocking_filter": "options_timing"}

    return {"allow": True, "adjusted_confidence": final_confidence,
            "reasons": reasons, "blocking_filter": None}


def get_filter_stats() -> dict:
    """Return current filter state for the dashboard."""
    with _sector_lock:
        sector_counts = {s: len(h) for s, h in _sector_direction_history.items() if h}
    with _opens_lock:
        cutoff = datetime.now() - timedelta(minutes=RAPID_FIRE_WINDOW_MIN)
        recent_opens_count = sum(1 for ts, _ in _recent_opens if ts >= cutoff)
    return {
        "sector_history": sector_counts,
        "recent_opens_60min": recent_opens_count,
        "rapid_fire_limit": RAPID_FIRE_MAX_OPENS,
        "sector_cooldown_hours": SECTOR_COOLDOWN_HOURS,
        "confidence_floor": CONFIDENCE_FLOOR,
    }
