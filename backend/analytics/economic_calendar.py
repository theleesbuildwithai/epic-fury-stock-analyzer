"""
Economic Calendar — pre-release sizing reduction + post-release regime refresh.

THE PROBLEM (from morning premarket check):
    NFP +172K (hot data) hit at 8:30 ET Friday. Picks cache was
    generated at 5:37 ET (before release). Yields jumped sharply at
    open. By the time Monday opens, the picks may still reflect
    pre-NFP macro state — leaving us exposed to outdated regime
    classification.

THIS MODULE PROVIDES:
1. Calendar of major releases through end of year (NFP, CPI, FOMC, etc.)
2. is_pre_release(): pause/reduce sizing 30 min before high-impact events
3. is_post_release(): detect within 90 min after release for refresh
4. get_active_event_impact(): returns risk modifier for current time

WHY NO PAID DATA NEEDED:
- Release SCHEDULE is publicly known months in advance
- Surprise DIRECTION is detected by macro_guard via yield day-move
- Combined: schedule tells us WHEN, macro_guard tells us WHAT.
"""
from datetime import datetime, timedelta
from typing import Optional


# ============================================================
# RELEASE SCHEDULE — major US economic events
# ============================================================
# All times in ET (US Eastern). All dates real for 2026.
#
# IMPACT levels:
#   HIGH:   FOMC decisions, NFP, CPI core misses — must reduce sizing
#   MEDIUM: PPI, retail sales, GDP advance — caution
#   LOW:    Confidence surveys — informational only

NFP_DATES_2026 = [
    "2026-01-09", "2026-02-06", "2026-03-06", "2026-04-03",
    "2026-05-01", "2026-06-05", "2026-07-03", "2026-08-07",
    "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04",
]

CPI_DATES_2026 = [
    "2026-01-13", "2026-02-12", "2026-03-12", "2026-04-10",
    "2026-05-12", "2026-06-11", "2026-07-15", "2026-08-12",
    "2026-09-10", "2026-10-15", "2026-11-12", "2026-12-10",
]

FOMC_MEETING_DATES_2026 = [  # Day 2 of meeting — decision day
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]

# Time-of-day (ET) for each release
RELEASE_TIMES = {
    "NFP": (8, 30),    # 8:30 AM ET
    "CPI": (8, 30),
    "PPI": (8, 30),
    "RETAIL_SALES": (8, 30),
    "GDP": (8, 30),
    "FOMC": (14, 0),   # 2:00 PM ET decision; press conf 2:30
    "JOBLESS_CLAIMS": (8, 30),  # Thursday weekly
}

# Pre-release pause window: minutes BEFORE release to reduce sizing
PRE_RELEASE_PAUSE_MINUTES = {
    "FOMC": 60,         # FOMC = de-risk 60 min before
    "NFP":  30,         # NFP = de-risk 30 min before
    "CPI":  30,
    "PPI":  15,
    "RETAIL_SALES": 15,
    "GDP":  15,
    "JOBLESS_CLAIMS": 0,  # Too noisy to react in advance
}

# Post-release refresh window: minutes AFTER release for picks regen
POST_RELEASE_REFRESH_MINUTES = {
    "FOMC": 90,
    "NFP":  60,
    "CPI":  60,
    "PPI":  30,
    "RETAIL_SALES": 30,
    "GDP":  30,
    "JOBLESS_CLAIMS": 0,
}

# Sizing multiplier during pre-release pause
PRE_RELEASE_SIZING_MULT = {
    "FOMC": 0.0,    # Don't open trades 1h before FOMC
    "NFP":  0.5,    # Half size for 30 min before NFP
    "CPI":  0.5,
    "PPI":  0.7,
    "RETAIL_SALES": 0.7,
    "GDP":  0.7,
    "JOBLESS_CLAIMS": 1.0,
}


def _get_et_now() -> datetime:
    """Returns current time in ET (handles UTC offset roughly)."""
    try:
        import pytz
        return datetime.now(pytz.timezone("US/Eastern")).replace(tzinfo=None)
    except Exception:
        # Fallback: UTC - 4h (ignores DST nuance, good enough for ±1h)
        return datetime.utcnow() - timedelta(hours=4)


def _release_datetime(date_str: str, event: str) -> Optional[datetime]:
    """Combine date + time-of-day into a datetime."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        h, m = RELEASE_TIMES.get(event, (8, 30))
        return d.replace(hour=h, minute=m)
    except (ValueError, KeyError):
        return None


def get_active_event() -> dict:
    """
    Returns whether a high-impact event is in pre-release pause OR
    post-release refresh window right now.

    Returns:
        {
            "in_pre_release_pause": bool,
            "in_post_release_window": bool,
            "active_event": str | None,    # e.g. "NFP", "FOMC"
            "minutes_to_release": int | None,
            "minutes_since_release": int | None,
            "sizing_multiplier": float,    # 0.0 to 1.0
            "should_refresh_picks": bool,
            "rationale": str,
        }
    """
    now = _get_et_now()
    today_str = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    # Check today and yesterday (to catch overnight post-release periods)
    candidates = []
    for date_str in [yesterday_str, today_str]:
        for event, dates in [("NFP", NFP_DATES_2026),
                              ("CPI", CPI_DATES_2026),
                              ("FOMC", FOMC_MEETING_DATES_2026)]:
            if date_str in dates:
                rt = _release_datetime(date_str, event)
                if rt:
                    candidates.append((event, rt))

    if not candidates:
        return _no_event_response()

    # Find the closest event in time
    best_event = None
    best_rt = None
    best_distance = float("inf")
    for event, rt in candidates:
        dt_minutes = abs((now - rt).total_seconds() / 60.0)
        if dt_minutes < best_distance:
            best_distance = dt_minutes
            best_event = event
            best_rt = rt

    if best_event is None or best_rt is None:
        return _no_event_response()

    minutes_to_release = int((best_rt - now).total_seconds() / 60.0)
    minutes_since_release = -minutes_to_release if minutes_to_release < 0 else None
    minutes_to_release = minutes_to_release if minutes_to_release >= 0 else None

    # Pre-release pause check
    pre_window = PRE_RELEASE_PAUSE_MINUTES.get(best_event, 0)
    in_pre_pause = (minutes_to_release is not None
                    and minutes_to_release > 0
                    and minutes_to_release <= pre_window)

    # Post-release window check
    post_window = POST_RELEASE_REFRESH_MINUTES.get(best_event, 0)
    in_post_window = (minutes_since_release is not None
                      and minutes_since_release >= 0
                      and minutes_since_release <= post_window)

    sizing_mult = (PRE_RELEASE_SIZING_MULT.get(best_event, 1.0)
                   if in_pre_pause else 1.0)

    should_refresh = in_post_window and minutes_since_release <= 90

    rationale = "no_active_event"
    if in_pre_pause:
        rationale = (f"PRE_{best_event}_PAUSE: release in {minutes_to_release}m, "
                     f"sizing mult={sizing_mult}")
    elif in_post_window:
        rationale = (f"POST_{best_event}_REFRESH: released {minutes_since_release}m ago, "
                     f"picks should be regenerated")

    return {
        "in_pre_release_pause": in_pre_pause,
        "in_post_release_window": in_post_window,
        "active_event": best_event,
        "minutes_to_release": minutes_to_release,
        "minutes_since_release": minutes_since_release,
        "sizing_multiplier": sizing_mult,
        "should_refresh_picks": should_refresh,
        "rationale": rationale,
    }


def _no_event_response() -> dict:
    return {
        "in_pre_release_pause": False,
        "in_post_release_window": False,
        "active_event": None,
        "minutes_to_release": None,
        "minutes_since_release": None,
        "sizing_multiplier": 1.0,
        "should_refresh_picks": False,
        "rationale": "no_active_event",
    }


def get_next_release(days_ahead: int = 14) -> dict:
    """
    Returns the next upcoming high-impact release within N days.
    Useful for systemic awareness.
    """
    now = _get_et_now()
    cutoff = now + timedelta(days=days_ahead)
    upcoming = []
    for event, dates in [("NFP", NFP_DATES_2026),
                          ("CPI", CPI_DATES_2026),
                          ("FOMC", FOMC_MEETING_DATES_2026)]:
        for date_str in dates:
            rt = _release_datetime(date_str, event)
            if rt and now < rt < cutoff:
                upcoming.append((event, rt))
    if not upcoming:
        return {"next_event": None, "minutes_until": None}
    upcoming.sort(key=lambda x: x[1])
    event, rt = upcoming[0]
    return {
        "next_event": event,
        "release_datetime_et": rt.strftime("%Y-%m-%d %H:%M ET"),
        "minutes_until": int((rt - now).total_seconds() / 60.0),
        "hours_until": round((rt - now).total_seconds() / 3600.0, 1),
        "days_until": round((rt - now).total_seconds() / 86400.0, 1),
    }


def is_high_impact_day(date: Optional[datetime] = None) -> bool:
    """True if the given date has any major release."""
    if date is None:
        date = _get_et_now()
    date_str = date.strftime("%Y-%m-%d")
    return (date_str in NFP_DATES_2026
            or date_str in CPI_DATES_2026
            or date_str in FOMC_MEETING_DATES_2026)
