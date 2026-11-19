"""Exchange-calendar-aware U.S. equity session classification.

Regular-session boundaries come from the XNYS exchange calendar, including
holidays and early closes. Extended-hours boundaries remain the scanner's
configured 04:00–20:00 America/New_York window.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

try:
    import exchange_calendars as xcals
    XNYS = xcals.get_calendar("XNYS")
except Exception:  # Keep the scanner available if optional calendar data cannot load.
    XNYS = None


def market_session_status(now=None):
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    eastern = current.astimezone(EASTERN)
    minute = eastern.hour * 60 + eastern.minute
    local_day = eastern.date()

    if XNYS is None:
        weekday = eastern.weekday() < 5
        if not weekday:
            phase = "CLOSED — WEEKEND"
        elif 240 <= minute < 570:
            phase = "PRE-MARKET"
        elif 570 <= minute < 960:
            phase = "REGULAR SESSION"
        elif 960 <= minute < 1200:
            phase = "AFTER-HOURS"
        else:
            phase = "CLOSED — OVERNIGHT"
        regular = bool(weekday and 570 <= minute < 960)
        extended = bool(weekday and (240 <= minute < 570 or 960 <= minute < 1200))
        return {
            "phase": phase, "is_regular_session": regular,
            "is_extended_session": extended, "as_of": eastern.isoformat(),
            "calendar_source": "weekday schedule fallback",
            "calendar_verified": False,
            "calendar_note": "Exchange calendar unavailable; holiday and early-close status is not verified.",
        }

    import pandas as pd

    session = pd.Timestamp(local_day)
    if not XNYS.is_session(session):
        phase = "CLOSED — WEEKEND" if eastern.weekday() >= 5 else "CLOSED — EXCHANGE HOLIDAY"
        return {
            "phase": phase, "is_regular_session": False, "is_extended_session": False,
            "as_of": eastern.isoformat(), "calendar_source": "XNYS exchange calendar",
            "calendar_verified": True,
            "calendar_note": "XNYS holidays and early closes are calendar-aware; extended-hours boundaries use the configured 04:00–20:00 ET window.",
        }

    market_open = XNYS.session_open(session).to_pydatetime().astimezone(EASTERN)
    market_close = XNYS.session_close(session).to_pydatetime().astimezone(EASTERN)
    premarket_open = market_open.replace(hour=4, minute=0, second=0, microsecond=0)
    afterhours_close = market_close.replace(hour=20, minute=0, second=0, microsecond=0)

    if market_open <= eastern < market_close:
        phase = "REGULAR SESSION"
    elif premarket_open <= eastern < market_open:
        phase = "PRE-MARKET"
    elif market_close <= eastern < afterhours_close:
        phase = "AFTER-HOURS"
    elif eastern < premarket_open:
        phase = "CLOSED — OVERNIGHT"
    else:
        phase = "CLOSED — OVERNIGHT"

    return {
        "phase": phase,
        "is_regular_session": bool(market_open <= eastern < market_close),
        "is_extended_session": bool(premarket_open <= eastern < market_open or market_close <= eastern < afterhours_close),
        "as_of": eastern.isoformat(),
        "regular_open": market_open.isoformat(),
        "regular_close": market_close.isoformat(),
        "early_close": market_close.hour < 16 or (market_close.hour == 16 and market_close.minute == 0 and market_close.date() == local_day and market_close.time() != datetime.strptime("16:00", "%H:%M").time()),
        "calendar_source": "XNYS exchange calendar",
        "calendar_verified": True,
        "calendar_note": "XNYS holidays and early closes are calendar-aware; extended-hours boundaries use the configured 04:00–20:00 ET window.",
    }
