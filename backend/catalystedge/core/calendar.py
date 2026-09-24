"""NYSE trading calendar (holidays and early closes) via exchange_calendars.

Every "next trading day" in CatalystEdge goes through here, so a Friday decision
executes Monday, a pre-holiday decision executes after the holiday, etc.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd

# EOD bars are treated as knowable this long after the official close.
EOD_AVAILABLE_AFTER_CLOSE = dt.timedelta(minutes=30)


@lru_cache(maxsize=1)
def _cal():
    return xcals.get_calendar("XNYS", start="1995-01-01")


def is_session(day: dt.date) -> bool:
    return bool(_cal().is_session(pd.Timestamp(day)))


def next_session(day: dt.date) -> dt.date:
    """The first trading day strictly after `day` (which need not be a session)."""
    cal = _cal()
    ts = pd.Timestamp(day)
    if cal.is_session(ts):
        return cal.next_session(ts).date()
    return cal.date_to_session(ts, direction="next").date()


def previous_session(day: dt.date) -> dt.date:
    """The last trading day strictly before `day`."""
    cal = _cal()
    ts = pd.Timestamp(day)
    if cal.is_session(ts):
        return cal.previous_session(ts).date()
    return cal.date_to_session(ts, direction="previous").date()


def add_sessions(day: dt.date, n: int) -> dt.date:
    """n trading days after `day` (n >= 1); `day` itself must be a session."""
    cal = _cal()
    return cal.session_offset(pd.Timestamp(day), n).date()


def sessions_between(start: dt.date, end: dt.date) -> list[dt.date]:
    """Trading days in [start, end]."""
    return [d.date() for d in _cal().sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))]


def session_open(day: dt.date) -> dt.datetime:
    return _cal().session_open(pd.Timestamp(day)).to_pydatetime()


def session_close(day: dt.date) -> dt.datetime:
    return _cal().session_close(pd.Timestamp(day)).to_pydatetime()


def eod_available_at(day: dt.date) -> dt.datetime:
    """When the daily bar for `day` may be used: official close (early closes included) + 30 min."""
    return session_close(day) + EOD_AVAILABLE_AFTER_CLOSE


def last_completed_session(now: dt.datetime) -> dt.date:
    """The latest session whose EOD bar is available at `now`."""
    day = now.astimezone(_cal().tz).date()
    if not is_session(day):
        return _cal().date_to_session(pd.Timestamp(day), direction="previous").date()
    return day if now >= eod_available_at(day) else previous_session(day)
