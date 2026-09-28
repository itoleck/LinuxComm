"""Three daily alarms (Alarm 1–3), saved in ~/linuxcomm/data/alarms.json.

An alarm has a time (to the minute) and rings every day while it is enabled. Snoozing
is kept in memory only; it never changes the saved alarm.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from .config import data_dir

log = logging.getLogger(__name__)

SLOTS = (1, 2, 3)
SNOOZE = dt.timedelta(minutes=10)
GRACE = dt.timedelta(minutes=5)          # an alarm missed by up to this much (a busy system) still rings
RING_LIMIT = dt.timedelta(minutes=15)    # ringing stops by itself after this long


@dataclass(frozen=True)
class Alarm:
    hour: int
    minute: int
    enabled: bool = True

    @property
    def time(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    def to_json(self) -> dict:
        return {"time": self.time, "enabled": self.enabled}


def parse_time(text: str) -> tuple[int, int]:
    """"07:30" -> (7, 30). ValueError messages are meant for the user."""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(text or ""))
    if not match:
        raise ValueError("Use a time like 07:30")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Use a time from 00:00 to 23:59")
    return hour, minute


def alarm_from_json(data) -> Alarm | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError("not an alarm")
    hour, minute = parse_time(data.get("time"))
    return Alarm(hour, minute, bool(data.get("enabled", True)))


def to_12h(hour: int) -> tuple[int, bool]:
    """24-hour hour -> (1..12, is_pm)."""
    return hour % 12 or 12, hour >= 12


def from_12h(hour12: int, pm: bool) -> int:
    return hour12 % 12 + (12 if pm else 0)


def format_time(hour: int, minute: int, clock_24h: bool) -> str:
    if clock_24h:
        return f"{hour:02d}:{minute:02d}"
    hour12, pm = to_12h(hour)
    return f"{hour12}:{minute:02d} {'PM' if pm else 'AM'}"


class AlarmStore:
    """The three alarms and their file. Thread-safe: the server changes them for remote stations."""

    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "alarms.json"
        self._lock = threading.RLock()
        self._alarms: dict[int, Alarm | None] = dict.fromkeys(SLOTS)
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("Ignoring unreadable alarms file %s: %s", self.path, e)
            return
        stored = data.get("alarms") if isinstance(data, dict) else None
        for slot in SLOTS:
            try:
                self._alarms[slot] = alarm_from_json((stored or {}).get(str(slot)))
            except (ValueError, TypeError, AttributeError):
                log.warning("Ignoring invalid Alarm %d in %s", slot, self.path)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"alarms": self.to_json()}, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def _check_slot(slot: int) -> None:
        if slot not in SLOTS:
            raise ValueError("There are alarms 1, 2 and 3")

    def get(self, slot: int) -> Alarm | None:
        with self._lock:
            return self._alarms.get(slot)

    def all(self) -> dict[int, Alarm | None]:
        with self._lock:
            return dict(self._alarms)

    def to_json(self) -> dict[str, dict | None]:
        with self._lock:
            return {str(s): (a.to_json() if a else None) for s, a in self._alarms.items()}

    def set(self, slot: int, hour: int, minute: int, enabled: bool = True) -> Alarm:
        """Create or change an alarm (and save the file)."""
        self._check_slot(slot)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("Use a time from 00:00 to 23:59")
        alarm = Alarm(hour, minute, bool(enabled))
        with self._lock:
            previous = self._alarms[slot]
            self._alarms[slot] = alarm
            try:
                self._save()
            except OSError:
                self._alarms[slot] = previous
                raise
        return alarm

    def delete(self, slot: int) -> None:
        self._check_slot(slot)
        with self._lock:
            previous = self._alarms[slot]
            self._alarms[slot] = None
            try:
                self._save()
            except OSError:
                self._alarms[slot] = previous
                raise


class AlarmClock:
    """Decides when alarms ring. Call due() regularly (the UI does, several times a second)."""

    def __init__(self, store: AlarmStore, now: dt.datetime):
        self.store = store
        self.snoozed: dict[int, dt.datetime] = {}  # slot -> when it rings again (memory only)
        self._last = now

    def due(self, now: dt.datetime) -> list[int]:
        """The alarms that start ringing between the previous call and `now`."""
        last, self._last = self._last, now
        if now < last or now - last > dt.timedelta(hours=12):
            return []  # the clock was changed, or the computer slept for hours: start over
        ringing = set()
        for slot, alarm in self.store.all().items():
            if alarm is None or not alarm.enabled:
                continue
            today = now.replace(hour=alarm.hour, minute=alarm.minute, second=0, microsecond=0)
            for when in (today, today - dt.timedelta(days=1)):  # the window may cross midnight
                if last < when <= now and now - when <= GRACE:
                    ringing.add(slot)
        for slot, when in list(self.snoozed.items()):
            if when <= now:
                del self.snoozed[slot]
                if now - when <= GRACE:
                    ringing.add(slot)
        return sorted(ringing)

    def snooze(self, slots, now: dt.datetime) -> dt.datetime:
        until = now + SNOOZE
        for slot in slots:
            self.snoozed[slot] = until
        return until

    def cancel(self, slot: int) -> None:
        """Forget a snooze (the alarm was changed, turned off or deleted)."""
        self.snoozed.pop(slot, None)
