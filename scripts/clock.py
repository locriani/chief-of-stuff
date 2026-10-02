"""Clock readings as the tracker writes them: `HH:MM`, a `HH:MM–HH:MM` range and a `YYYY-MM-DD[ HH:MM]` date."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")
# Accept both separators in a completed time range.
RAN = re.compile(r"^(\d{1,2}:\d{2})[–-](\d{1,2}:\d{2})$")
DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}))?$")


def hhmm(value: str, day: date, zone: ZoneInfo) -> datetime | None:
    m = HHMM.match(value.strip())
    try:
        return datetime.combine(day, time(int(m[1]), int(m[2])), tzinfo=zone) if m else None
    except ValueError:  # 24:00, 9:75: not a clock, so no clock
        return None


def done_clock(value: str, now: datetime, zone: ZoneInfo) -> datetime | None:
    """(#213) A done task's bare `HH:MM` is its latest occurrence at or before `now`: today's, unless
    that is still later than `now`, in which case it hasn't happened yet and means yesterday's."""
    t = hhmm(value, now.date(), zone)
    return t if t is None or t <= now else t - timedelta(days=1)


def dur(td: timedelta) -> str:
    h, m = divmod(int(td.total_seconds()) // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"
