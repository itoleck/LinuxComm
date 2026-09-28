"""Tests for alarms: the saved file, times, and when alarms ring.

Run with:  python3 -m unittest discover -s tests -v
"""

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linuxcomm import alarms  # noqa: E402

T = dt.datetime  # local time, like datetime.now()


class TimeTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(alarms.parse_time("07:30"), (7, 30))
        self.assertEqual(alarms.parse_time(" 7:05 "), (7, 5))
        for bad in ("", "7", "24:00", "12:60", "7:5", "noon", None):
            with self.assertRaises(ValueError, msg=bad):
                alarms.parse_time(bad)

    def test_12_hour_clock(self):
        self.assertEqual([alarms.to_12h(h) for h in (0, 1, 11, 12, 13, 23)],
                         [(12, False), (1, False), (11, False), (12, True), (1, True), (11, True)])
        for hour in range(24):
            self.assertEqual(alarms.from_12h(*alarms.to_12h(hour)), hour)
        self.assertEqual(alarms.format_time(7, 5, True), "07:05")
        self.assertEqual(alarms.format_time(0, 30, False), "12:30 AM")
        self.assertEqual(alarms.format_time(19, 0, False), "7:00 PM")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "data" / "alarms.json"

    def test_create_change_disable_delete_and_reload(self):
        store = alarms.AlarmStore(self.path)
        self.assertEqual(store.all(), {1: None, 2: None, 3: None})
        self.assertFalse(self.path.exists())
        store.set(1, 7, 30)                       # created: saved right away
        store.set(3, 22, 0, enabled=False)
        self.assertEqual(json.loads(self.path.read_text())["alarms"],
                         {"1": {"time": "07:30", "enabled": True}, "2": None,
                          "3": {"time": "22:00", "enabled": False}})
        store.set(1, 6, 45)                       # changed
        store.delete(3)
        again = alarms.AlarmStore(self.path)      # the next run of the app
        self.assertEqual(again.get(1), alarms.Alarm(6, 45, True))
        self.assertIsNone(again.get(3))

    def test_invalid_input(self):
        store = alarms.AlarmStore(self.path)
        for args in ((4, 7, 0), (0, 7, 0), (1, 24, 0), (1, 7, 60)):
            with self.assertRaises(ValueError, msg=args):
                store.set(*args)

    def test_damaged_file(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"alarms": {"1": {"time": "25:99"}, "2": {"time": "08:15", "enabled": false}}}')
        store = alarms.AlarmStore(self.path)
        self.assertEqual(store.all(), {1: None, 2: alarms.Alarm(8, 15, False), 3: None})
        self.path.write_text("not json")
        self.assertEqual(alarms.AlarmStore(self.path).all(), {1: None, 2: None, 3: None})


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.store = alarms.AlarmStore(Path(tempfile.mkdtemp()) / "alarms.json")
        self.store.set(1, 7, 30)

    def run_clock(self, start, end, step=dt.timedelta(seconds=0.25)):
        """Tick like the UI does; return (time, slots) for every time something rang."""
        clock = alarms.AlarmClock(self.store, start)
        rang, now = [], start
        while now < end:
            now += step
            slots = clock.due(now)
            if slots:
                rang.append((now, slots))
        return clock, rang

    def test_rings_once_at_the_minute_every_day(self):
        _, rang = self.run_clock(T(2026, 9, 28, 7, 0), T(2026, 9, 30, 8, 0), step=dt.timedelta(seconds=15))
        self.assertEqual([(t.date(), t.hour, t.minute, s) for t, s in rang],
                         [(dt.date(2026, 9, 28), 7, 30, [1]), (dt.date(2026, 9, 29), 7, 30, [1]),
                          (dt.date(2026, 9, 30), 7, 30, [1])])

    def test_disabled_and_deleted_alarms_do_not_ring(self):
        self.store.set(1, 7, 30, enabled=False)
        self.store.set(2, 7, 31)
        self.store.delete(2)
        _, rang = self.run_clock(T(2026, 9, 28, 7, 0), T(2026, 9, 28, 8, 0), step=dt.timedelta(seconds=15))
        self.assertEqual(rang, [])

    def test_several_alarms_at_the_same_minute(self):
        self.store.set(3, 7, 30)
        _, rang = self.run_clock(T(2026, 9, 28, 7, 29), T(2026, 9, 28, 7, 31), step=dt.timedelta(seconds=1))
        self.assertEqual([s for _, s in rang], [[1, 3]])

    def test_midnight(self):
        self.store.set(1, 0, 0)
        _, rang = self.run_clock(T(2026, 9, 28, 23, 59, 50), T(2026, 9, 29, 0, 0, 10), step=dt.timedelta(seconds=1))
        self.assertEqual([(t.day, t.hour, t.minute) for t, _ in rang], [(29, 0, 0)])

    def test_starting_the_app_during_the_alarm_minute_does_not_ring(self):
        _, rang = self.run_clock(T(2026, 9, 28, 7, 30, 20), T(2026, 9, 28, 7, 40))
        self.assertEqual(rang, [])

    def test_a_short_delay_still_rings_but_a_long_sleep_does_not(self):
        clock = alarms.AlarmClock(self.store, T(2026, 9, 28, 7, 29))
        self.assertEqual(clock.due(T(2026, 9, 28, 7, 33)), [1])   # 3 minutes late: rings
        clock = alarms.AlarmClock(self.store, T(2026, 9, 28, 7, 0))
        self.assertEqual(clock.due(T(2026, 9, 28, 9, 0)), [])     # 90 minutes late: too late

    def test_clock_set_back_does_not_ring(self):
        clock = alarms.AlarmClock(self.store, T(2026, 9, 28, 8, 0))
        self.assertEqual(clock.due(T(2026, 9, 28, 7, 30, 30)), [])

    def test_snooze(self):
        clock = alarms.AlarmClock(self.store, T(2026, 9, 28, 7, 29, 59))
        self.assertEqual(clock.due(T(2026, 9, 28, 7, 30)), [1])
        until = clock.snooze([1], T(2026, 9, 28, 7, 30, 20))
        self.assertEqual(until, T(2026, 9, 28, 7, 40, 20))
        self.assertEqual(clock.due(T(2026, 9, 28, 7, 40, 19)), [])
        self.assertEqual(clock.due(T(2026, 9, 28, 7, 40, 20)), [1])   # rings again after 10 minutes
        self.assertEqual(clock.due(T(2026, 9, 28, 7, 40, 30)), [])     # once
        self.assertEqual(self.store.get(1), alarms.Alarm(7, 30, True), "snoozing doesn't change the alarm")

    def test_cancelled_snooze(self):
        clock = alarms.AlarmClock(self.store, T(2026, 9, 28, 7, 29, 59))
        clock.due(T(2026, 9, 28, 7, 30))
        clock.snooze([1], T(2026, 9, 28, 7, 30))
        clock.cancel(1)                                                 # e.g. the alarm was turned off
        self.assertEqual(clock.due(T(2026, 9, 28, 7, 41)), [])


if __name__ == "__main__":
    unittest.main()
