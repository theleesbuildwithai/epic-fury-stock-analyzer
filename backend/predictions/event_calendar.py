"""
Macro Event Calendar — forward-looking risk awareness.

Pulls upcoming high-impact macro events (FOMC, CPI, NFP, GDP, PPI)
and lets the system adjust position sizing 24h before. Historical:
SPY moves ~±1.2% on FOMC days, ~±0.8% on CPI days. Trading INTO
these events without hedge is consistently worse than waiting.

KNOWN EVENT TYPES + TYPICAL VOL IMPACT:
  FOMC      | ±1.2% SPY same-day, often catalyst for regime change
  CPI       | ±0.8% SPY, magnified during inflation regimes
  NFP       | ±0.6% SPY, magnified during late-cycle
  GDP       | ±0.4% SPY, less reactive
  PPI       | ±0.3% SPY, secondary signal

PIPELINE:
  1. Static schedule of FOMC + CPI release dates (we hardcode 2026
     since these are deterministic government dates)
  2. get_upcoming_events(days_ahead) — returns events in window
  3. get_risk_adjustment_factor() — returns 1.0 normally, 0.7 if
     high-impact event in <24h, 0.5 if FOMC in <24h
  4. is_event_blackout_window() — bool: should we pause new positions?

SAFETY:
  - Pure data + lookup, no external calls (so no failure modes)
  - Static dates updated annually
  - Returns conservative neutral defaults if config missing
"""

import logging
from datetime import datetime, timedelta, date

logger = logging.getLogger(__name__)

# 2026 macro event calendar (US-centric, high-impact only)
# All dates in YYYY-MM-DD. Times are approximate ET release.
# This list should be updated each year — fail-safe is to return
# empty list (= no event awareness, normal sizing).
MACRO_EVENTS_2026 = [
    # FOMC meetings (Tue-Wed, decision Wed 2pm ET)
    {"date": "2026-01-28", "type": "FOMC",  "label": "January FOMC", "impact": "high"},
    {"date": "2026-03-18", "type": "FOMC",  "label": "March FOMC + SEP", "impact": "high"},
    {"date": "2026-05-06", "type": "FOMC",  "label": "May FOMC", "impact": "high"},
    {"date": "2026-06-17", "type": "FOMC",  "label": "June FOMC + SEP", "impact": "high"},
    {"date": "2026-07-29", "type": "FOMC",  "label": "July FOMC", "impact": "high"},
    {"date": "2026-09-16", "type": "FOMC",  "label": "September FOMC + SEP", "impact": "high"},
    {"date": "2026-11-04", "type": "FOMC",  "label": "November FOMC", "impact": "high"},
    {"date": "2026-12-16", "type": "FOMC",  "label": "December FOMC + SEP", "impact": "high"},
    # CPI releases (~8:30am ET, 2nd Tue/Wed of month)
    {"date": "2026-01-13", "type": "CPI",   "label": "December CPI", "impact": "high"},
    {"date": "2026-02-11", "type": "CPI",   "label": "January CPI", "impact": "high"},
    {"date": "2026-03-11", "type": "CPI",   "label": "February CPI", "impact": "high"},
    {"date": "2026-04-14", "type": "CPI",   "label": "March CPI", "impact": "high"},
    {"date": "2026-05-12", "type": "CPI",   "label": "April CPI", "impact": "high"},
    {"date": "2026-06-10", "type": "CPI",   "label": "May CPI", "impact": "high"},
    {"date": "2026-07-14", "type": "CPI",   "label": "June CPI", "impact": "high"},
    {"date": "2026-08-12", "type": "CPI",   "label": "July CPI", "impact": "high"},
    {"date": "2026-09-10", "type": "CPI",   "label": "August CPI", "impact": "high"},
    {"date": "2026-10-14", "type": "CPI",   "label": "September CPI", "impact": "high"},
    {"date": "2026-11-12", "type": "CPI",   "label": "October CPI", "impact": "high"},
    {"date": "2026-12-10", "type": "CPI",   "label": "November CPI", "impact": "high"},
    # NFP releases (1st Friday of month, 8:30am ET)
    {"date": "2026-01-09", "type": "NFP",   "label": "December NFP", "impact": "medium"},
    {"date": "2026-02-06", "type": "NFP",   "label": "January NFP", "impact": "medium"},
    {"date": "2026-03-06", "type": "NFP",   "label": "February NFP", "impact": "medium"},
    {"date": "2026-04-03", "type": "NFP",   "label": "March NFP", "impact": "medium"},
    {"date": "2026-05-01", "type": "NFP",   "label": "April NFP", "impact": "medium"},
    {"date": "2026-06-05", "type": "NFP",   "label": "May NFP", "impact": "medium"},
    {"date": "2026-07-02", "type": "NFP",   "label": "June NFP", "impact": "medium"},
    {"date": "2026-08-07", "type": "NFP",   "label": "July NFP", "impact": "medium"},
    {"date": "2026-09-04", "type": "NFP",   "label": "August NFP", "impact": "medium"},
    {"date": "2026-10-02", "type": "NFP",   "label": "September NFP", "impact": "medium"},
    {"date": "2026-11-06", "type": "NFP",   "label": "October NFP", "impact": "medium"},
    {"date": "2026-12-04", "type": "NFP",   "label": "November NFP", "impact": "medium"},
]

# Historical typical vol per event type (used for sizing math)
TYPICAL_VOL_BY_TYPE = {
    "FOMC": 1.20,   # %
    "CPI":  0.80,
    "NFP":  0.60,
    "GDP":  0.40,
    "PPI":  0.30,
}


def get_upcoming_events(days_ahead: int = 14) -> dict:
    """Returns events in the next N days. Sorted soonest-first."""
    try:
        today = date.today()
        cutoff = today + timedelta(days=int(days_ahead))
        upcoming = []
        for ev in MACRO_EVENTS_2026:
            try:
                ev_date = datetime.strptime(ev["date"], "%Y-%m-%d").date()
                if today <= ev_date <= cutoff:
                    days_until = (ev_date - today).days
                    upcoming.append({
                        **ev,
                        "days_until": days_until,
                        "typical_vol_pct": TYPICAL_VOL_BY_TYPE.get(ev["type"], 0.5),
                    })
            except Exception:
                continue
        upcoming.sort(key=lambda e: e["date"])
        return {
            "ok": True,
            "today": today.isoformat(),
            "days_ahead_window": days_ahead,
            "count": len(upcoming),
            "events": upcoming,
        }
    except Exception as e:
        logger.warning(f"get_upcoming_events fail: {e}")
        return {"ok": False, "reason": str(e)[:200], "events": []}


def get_risk_adjustment_factor() -> dict:
    """Returns the recommended position-size multiplier given upcoming events.
    Logic:
      - FOMC in <24h            -> 0.50 (cut size in half)
      - High-impact in <24h     -> 0.70
      - High-impact in 24-48h   -> 0.85
      - Otherwise               -> 1.00 (normal)
    NEVER raises — soft-fails to neutral 1.0."""
    try:
        upcoming = get_upcoming_events(days_ahead=3)
        if not upcoming.get("ok"):
            return {"ok": False, "factor": 1.0, "reason": "upcoming_events_failed"}
        events = upcoming.get("events", [])
        if not events:
            return {"ok": True, "factor": 1.0, "reason": "no_events_within_3d"}

        soonest = events[0]
        days_until = soonest["days_until"]
        ev_type = soonest["type"]
        impact = soonest.get("impact", "medium")

        if days_until == 0 or days_until == 1:
            if ev_type == "FOMC":
                factor = 0.50
                reason = (f"FOMC in {days_until}d — cut size 50% "
                          f"(typical SPY move ±1.2%)")
            elif impact == "high":
                factor = 0.70
                reason = f"High-impact {ev_type} in {days_until}d — cut size 30%"
            else:
                factor = 0.85
                reason = f"Medium-impact {ev_type} in {days_until}d"
        elif days_until == 2 and impact == "high":
            factor = 0.85
            reason = f"High-impact {ev_type} in 2 days — cut size 15%"
        else:
            factor = 1.0
            reason = f"Next event ({ev_type}) in {days_until}d — normal sizing"

        return {
            "ok": True,
            "factor": factor,
            "next_event": soonest,
            "reason": reason,
        }
    except Exception as e:
        logger.debug(f"get_risk_adjustment_factor soft-fail: {e}")
        return {"ok": False, "factor": 1.0, "reason": str(e)[:200]}


def is_event_blackout_window(hours: int = 12) -> dict:
    """Returns whether we're inside an event blackout (don't open new
    positions). Window: `hours` BEFORE next FOMC/CPI."""
    try:
        upcoming = get_upcoming_events(days_ahead=2)
        if not upcoming.get("ok") or not upcoming.get("events"):
            return {"in_blackout": False}
        soonest = upcoming["events"][0]
        if soonest["type"] not in ("FOMC", "CPI"):
            return {"in_blackout": False, "next_event": soonest}
        days_until = soonest["days_until"]
        # Approximate: if days_until == 0, we are within the window;
        # if days_until == 1 and current time < (24-hours), still in window
        if days_until == 0:
            return {"in_blackout": True, "event": soonest,
                    "reason": f"{soonest['type']} today"}
        if days_until == 1:
            # If less than `hours` to midnight ET, conservatively in window
            hour_now = datetime.utcnow().hour
            # Past 12pm UTC ≈ within a window for tomorrow ET morning
            if hour_now >= (24 - hours):
                return {"in_blackout": True, "event": soonest,
                        "reason": f"{soonest['type']} tomorrow"}
        return {"in_blackout": False, "next_event": soonest}
    except Exception as e:
        logger.debug(f"is_event_blackout_window soft-fail: {e}")
        return {"in_blackout": False, "reason": str(e)[:200]}
