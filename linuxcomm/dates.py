"""Date arithmetic for the calendars (no GTK, so it can be tested on its own)."""

from __future__ import annotations

import datetime as dt
import subprocess
from functools import lru_cache

MONDAY, SUNDAY = 0, 6  # Python weekday numbers


def parse_first_weekday(week_1stday: str, first_weekday: str) -> int:
    """glibc's LC_TIME week-1stday (a date) and first_weekday (1-7, counted from it) -> Python weekday."""
    base = dt.datetime.strptime(week_1stday, "%Y%m%d").date()
    return (base.weekday() + int(first_weekday) - 1) % 7


@lru_cache(maxsize=1)
def first_weekday() -> int:
    """First day of the week in the user's locale (Sunday in the US, Monday in most of Europe)."""
    try:
        out = subprocess.run(["locale", "week-1stday", "first_weekday"], capture_output=True,
                             text=True, timeout=2).stdout.split()
        return parse_first_weekday(out[0], out[1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return MONDAY  # ISO 8601


def week_from(day: dt.date, count: int = 7) -> list[dt.date]:
    """`day` and the days after it."""
    return [day + dt.timedelta(days=i) for i in range(count)]


def month_grid(year: int, month: int, week_start: int) -> list[list[dt.date]]:
    """Six weeks of dates covering the month, each week starting on `week_start`.

    Always six rows, so the calendar keeps the same height from month to month.
    """
    first = dt.date(year, month, 1)
    start = first - dt.timedelta(days=(first.weekday() - week_start) % 7)
    return [week_from(start + dt.timedelta(weeks=row)) for row in range(6)]


def add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1
