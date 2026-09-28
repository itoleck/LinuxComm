"""Tests for the calendar date arithmetic (no GTK needed).

Run with:  python3 -m unittest discover -s tests -v
"""

import calendar
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linuxcomm import dates  # noqa: E402


class FirstWeekdayTests(unittest.TestCase):
    def test_glibc_locale_values(self):
        self.assertEqual(dates.parse_first_weekday("19971130", "1"), dates.SUNDAY)  # en_US
        self.assertEqual(dates.parse_first_weekday("19971130", "2"), dates.MONDAY)  # en_GB, de_DE
        self.assertEqual(dates.parse_first_weekday("19971201", "1"), dates.MONDAY)  # Monday-based locales
        self.assertEqual(dates.parse_first_weekday("19971130", "7"), 5)             # Saturday (e.g. ar_EG)

    def test_system_value_is_a_weekday(self):
        self.assertIn(dates.first_weekday(), range(7))


class MonthGridTests(unittest.TestCase):
    def check_grid(self, year, month, week_start):
        grid = dates.month_grid(year, month, week_start)
        self.assertEqual(len(grid), 6)
        days = [d for week in grid for d in week]
        self.assertEqual(len(days), 42)
        self.assertTrue(all(week[0].weekday() == week_start for week in grid))
        self.assertTrue(all(b - a == dt.timedelta(days=1) for a, b in zip(days, days[1:])))
        in_month = [d for d in days if d.month == month]
        self.assertEqual(in_month[0], dt.date(year, month, 1))
        self.assertEqual(len(in_month), calendar.monthrange(year, month)[1])
        return grid

    def test_september_2026(self):  # starts on a Tuesday
        self.assertEqual(self.check_grid(2026, 9, dates.SUNDAY)[0][0], dt.date(2026, 8, 30))
        self.assertEqual(self.check_grid(2026, 9, dates.MONDAY)[0][0], dt.date(2026, 8, 31))

    def test_month_starting_on_the_first_weekday(self):
        self.assertEqual(self.check_grid(2026, 2, dates.SUNDAY)[0][0], dt.date(2026, 2, 1))  # Feb 1 2026 is a Sunday
        self.assertEqual(self.check_grid(2026, 6, dates.MONDAY)[0][0], dt.date(2026, 6, 1))  # Jun 1 2026 is a Monday

    def test_every_month_for_several_years(self):
        for year in (2024, 2025, 2026, 2027, 2028):  # includes leap years
            for month in range(1, 13):
                for start in range(7):
                    self.check_grid(year, month, start)


class HelperTests(unittest.TestCase):
    def test_add_months(self):
        self.assertEqual(dates.add_months(2026, 12, 1), (2027, 1))
        self.assertEqual(dates.add_months(2026, 1, -1), (2025, 12))
        self.assertEqual(dates.add_months(2026, 9, -21), (2024, 12))
        self.assertEqual(dates.add_months(2026, 9, 0), (2026, 9))

    def test_week_from_crosses_the_year(self):
        week = dates.week_from(dt.date(2026, 12, 29))
        self.assertEqual([d.day for d in week], [29, 30, 31, 1, 2, 3, 4])
        self.assertEqual(week[3], dt.date(2027, 1, 1))


if __name__ == "__main__":
    unittest.main()
