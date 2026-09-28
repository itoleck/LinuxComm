"""Calendar widgets: the 7-day strip under the clock and the month calendar panel."""

from __future__ import annotations

import datetime as dt
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from . import dates  # noqa: E402

CSS = """
.week-strip { margin-top: 14px; }
.week-day { padding: 6px 0; border-radius: 12px; }
.week-day .weekday { font-size: 9pt; font-weight: bold; opacity: 0.6; }
.week-day .day-number { font-size: 14pt; }
.week-day.today, .month-day.today { background-color: @accent_bg_color; color: @accent_fg_color; }
.week-day.today .weekday { opacity: 1; }
.calendar-panel { padding: 16px 18px; }
.calendar-title { font-size: 16pt; font-weight: bold; }
.month-weekday { font-size: 10pt; font-weight: bold; opacity: 0.6; }
.month-day { min-width: 40px; min-height: 40px; border-radius: 999px; font-size: 13pt; }
.month-day.other-month { opacity: 0.35; }
.narrow .calendar-panel { padding: 12px; }
.narrow .calendar-title { font-size: 13pt; }
.narrow .month-day { min-width: 34px; min-height: 34px; font-size: 12pt; }
"""


def _glib_date(day: dt.date) -> GLib.DateTime:
    return GLib.DateTime.new_local(day.year, day.month, day.day, 12, 0, 0)


def weekday_name(day: dt.date) -> str:
    return _glib_date(day).format("%a")  # localized, e.g. "Mon", "Mo."


def long_date(day: dt.date) -> str:
    return _glib_date(day).format("%A, %B %-d")


def month_title(year: int, month: int) -> str:
    # %OB is the standalone month name, which differs from %B in e.g. Polish and Russian.
    return _glib_date(dt.date(year, month, 1)).format("%OB %Y")


class WeekStrip(Gtk.Box):
    """Today and the next six days, with today highlighted."""

    def __init__(self):
        super().__init__(homogeneous=True, spacing=4, css_classes=["week-strip"])
        self._cells = []
        for _ in range(7):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["week-day"])
            name = Gtk.Label(css_classes=["weekday"])
            number = Gtk.Label(css_classes=["day-number"])
            cell.append(name)
            cell.append(number)
            self.append(cell)
            self._cells.append((cell, name, number))
        self._shown: dt.date | None = None
        self.update()

    def update(self) -> None:
        today = dt.date.today()
        if today == self._shown:
            return
        self._shown = today
        for i, (day, (cell, name, number)) in enumerate(zip(dates.week_from(today), self._cells)):
            name.set_label(weekday_name(day))
            number.set_label(str(day.day))
            cell.set_tooltip_text(("Today, " if i == 0 else "") + long_date(day))
            (cell.add_css_class if i == 0 else cell.remove_css_class)("today")


class MonthCalendar(Gtk.Box):
    """A month calendar with today highlighted; ← → (or a swipe) change the month."""

    def __init__(self, on_close: Callable[[], None]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12, css_classes=["card", "calendar-panel"])
        header = Gtk.Box(spacing=6)
        self._prev = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Previous month",
                                css_classes=["flat", "circular"], valign=Gtk.Align.CENTER)
        self._prev.connect("clicked", lambda *_: self.change_month(-1))
        self._title = Gtk.Label(hexpand=True, xalign=0, css_classes=["calendar-title"],
                                ellipsize=Pango.EllipsizeMode.END)
        self._next = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="Next month",
                                css_classes=["flat", "circular"], valign=Gtk.Align.CENTER)
        self._next.connect("clicked", lambda *_: self.change_month(1))
        self._today = Gtk.Button(label="Today", valign=Gtk.Align.CENTER)
        self._today.connect("clicked", lambda *_: self.show_today())
        self.close_button = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Close calendar (Esc)",
                                       css_classes=["flat", "circular"], valign=Gtk.Align.CENTER)
        self.close_button.connect("clicked", lambda *_: on_close())
        for widget in (self._title, self._prev, self._next, self._today, self.close_button):
            header.append(widget)
        self.append(header)

        grid = Gtk.Grid(row_homogeneous=True, column_homogeneous=True, row_spacing=2, column_spacing=2, vexpand=True)
        self._weekday_labels = [Gtk.Label(css_classes=["month-weekday"]) for _ in range(7)]
        for column, label in enumerate(self._weekday_labels):
            grid.attach(label, column, 0, 1, 1)
        self._day_labels = []
        for row in range(6):
            week = []
            for column in range(7):
                label = Gtk.Label(css_classes=["month-day"], halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
                grid.attach(label, column, row + 1, 1, 1)
                week.append(label)
            self._day_labels.append(week)
        self.append(grid)

        swipe = Gtk.GestureSwipe(touch_only=True)
        swipe.connect("swipe", self._on_swipe)
        grid.add_controller(swipe)

        self._year, self._month = dt.date.today().year, dt.date.today().month
        self.show_today()

    def show_today(self) -> None:
        today = dt.date.today()
        self.show_month(today.year, today.month)

    def change_month(self, delta: int) -> None:
        self.show_month(*dates.add_months(self._year, self._month, delta))

    def show_month(self, year: int, month: int) -> None:
        self._year, self._month = year, month
        today = dt.date.today()
        self._title.set_label(month_title(year, month))
        self._today.set_visible((year, month) != (today.year, today.month))  # only when away from today
        weeks = dates.month_grid(year, month, dates.first_weekday())
        for label, day in zip(self._weekday_labels, weeks[0]):
            label.set_label(weekday_name(day))
        for week, labels in zip(weeks, self._day_labels):
            for day, label in zip(week, labels):
                label.set_label(str(day.day))
                label.set_tooltip_text(long_date(day))
                (label.add_css_class if day.month != month else label.remove_css_class)("other-month")
                (label.add_css_class if day == today else label.remove_css_class)("today")

    def refresh(self) -> None:
        """Re-mark today (after midnight) without leaving the month being viewed."""
        self.show_month(self._year, self._month)

    def _on_swipe(self, _gesture, velocity_x: float, velocity_y: float) -> None:
        if abs(velocity_x) > 300 and abs(velocity_x) > 2 * abs(velocity_y):
            self.change_month(1 if velocity_x < 0 else -1)
