"""Alarm widgets: the three cells above the clock, the settings panel, and the ringing panel."""

from __future__ import annotations

import datetime as dt
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from .alarms import SLOTS, Alarm, format_time, from_12h, to_12h  # noqa: E402

CSS = """
.alarm-cells { margin-bottom: 6px; }
.alarm-cell { padding: 4px 6px; border-radius: 10px; font-weight: normal; }
.alarm-cell .alarm-name { font-size: 8.5pt; font-weight: bold; }
.alarm-cell .alarm-value { font-size: 12pt; }
.alarm-cell.on .alarm-value { color: @accent_color; font-weight: bold; }
.alarm-cell.off .alarm-value, .alarm-cell.empty .alarm-value { opacity: 0.55; }
.alarm-cell.snoozed .alarm-value { font-style: italic; }
.alarm-cell.ringing, .alarm-cell.ringing .alarm-name { background-color: @accent_bg_color; color: @accent_fg_color; }
.alarm-spin { font-size: 26pt; }
.alarm-spin-separator { font-size: 26pt; font-weight: bold; }
.alarm-ringing-time { font-size: 48pt; font-weight: 300; }
.alarm-ringing-button { min-height: 56px; font-size: 14pt; }
"""

ALARM_ICONS = ("alarm-symbolic", "appointment-soon-symbolic", "preferences-system-notifications-symbolic")


def alarm_icon(widget: Gtk.Widget) -> str:
    theme = Gtk.IconTheme.get_for_display(widget.get_display())
    return next((name for name in ALARM_ICONS if theme.has_icon(name)), ALARM_ICONS[-1])


class AlarmCells(Gtk.Box):
    """Alarm 1–3 above the time; clicking one opens its settings."""

    def __init__(self, on_open: Callable[[int], None]):
        super().__init__(homogeneous=True, spacing=6, css_classes=["alarm-cells"])
        self._cells: dict[int, tuple[Gtk.Button, Gtk.Label]] = {}
        for slot in SLOTS:
            name = Gtk.Label(label=f"Alarm {slot}", css_classes=["alarm-name", "dim-label"])
            value = Gtk.Label(css_classes=["alarm-value"], ellipsize=Pango.EllipsizeMode.END)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            box.append(name)
            box.append(value)
            button = Gtk.Button(child=box, css_classes=["flat", "alarm-cell"])
            button.connect("clicked", lambda *_, s=slot: on_open(s))
            self.append(button)
            self._cells[slot] = (button, value)

    def update(self, slot: int, alarm: Alarm | None, clock_24h: bool,
               snoozed_until: dt.datetime | None = None, ringing: bool = False) -> None:
        button, value = self._cells[slot]
        if ringing:
            state, text = "ringing", "Ringing"
        elif snoozed_until is not None:
            state, text = "snoozed", f"Snoozed · {format_time(snoozed_until.hour, snoozed_until.minute, clock_24h)}"
        elif alarm is None:
            state, text = "empty", "Not set"
        else:
            time_text = format_time(alarm.hour, alarm.minute, clock_24h)
            state, text = ("on", time_text) if alarm.enabled else ("off", f"{time_text} · Off")
        value.set_label(text)
        for css in ("ringing", "snoozed", "empty", "on", "off"):
            (button.add_css_class if css == state else button.remove_css_class)(css)
        tips = {"ringing": "Ringing now", "snoozed": "Snoozed", "empty": "Set an alarm",
                "on": "Rings every day", "off": "Turned off"}
        button.set_tooltip_text(f"Alarm {slot}: {tips[state]}")


def _spin(upper: int, lower: int = 0) -> Gtk.SpinButton:
    spin = Gtk.SpinButton.new_with_range(lower, upper, 1)
    spin.set_orientation(Gtk.Orientation.VERTICAL)  # + above, − below: easy to use on a touchscreen
    spin.set_wrap(True)
    spin.set_numeric(True)
    spin.add_css_class("alarm-spin")
    spin.connect("output", lambda s: (s.set_text(f"{int(s.get_value()):02d}"), True)[1])
    return spin


class AlarmEditor(Gtk.Box):
    """Settings for Alarm 1–3 of this station or another one, shown in the calendar's place."""

    def __init__(self, on_close: Callable[[], None], on_select: Callable[[int], None],
                 on_save: Callable[[int, int, int, bool], None], on_delete: Callable[[int], None],
                 on_retry: Callable[[], None]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                         css_classes=["card", "calendar-panel", "alarm-editor"])
        self._on_select = on_select
        self._clock_24h = True
        self._syncing = False

        header = Gtk.Box(spacing=6)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self._title = Gtk.Label(label="Alarms", xalign=0, css_classes=["calendar-title"],
                                ellipsize=Pango.EllipsizeMode.END)
        self._subtitle = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"], ellipsize=Pango.EllipsizeMode.END)
        titles.append(self._title)
        titles.append(self._subtitle)
        header.append(titles)
        self.close_button = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Close (Esc)",
                                       css_classes=["flat", "circular"], valign=Gtk.Align.CENTER)
        self.close_button.connect("clicked", lambda *_: on_close())
        header.append(self.close_button)
        self.append(header)

        slots = Gtk.Box(css_classes=["linked"], homogeneous=True, halign=Gtk.Align.FILL)
        self._slot_buttons: dict[int, Gtk.ToggleButton] = {}
        for slot in SLOTS:
            button = Gtk.ToggleButton(label=f"Alarm {slot}")
            if self._slot_buttons:
                button.set_group(self._slot_buttons[SLOTS[0]])
            button.connect("toggled", self._slot_toggled, slot)
            slots.append(button)
            self._slot_buttons[slot] = button
        self.append(slots)

        self._stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.append(self._stack)

        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER)
        loading.append(Gtk.Spinner(spinning=True, width_request=32, height_request=32))
        self._loading_label = Gtk.Label(css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER)
        loading.append(self._loading_label)
        self._stack.add_named(loading, "loading")

        error = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER)
        self._error_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        retry = Gtk.Button(label="Try again", halign=Gtk.Align.CENTER, css_classes=["pill"])
        retry.connect("clicked", lambda *_: on_retry())
        error.append(self._error_label)
        error.append(retry)
        self._stack.add_named(error, "error")

        edit = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, valign=Gtk.Align.CENTER)
        self._status = Gtk.Label(css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER)
        edit.append(self._status)
        picker = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self._hour = _spin(23)
        self._minute = _spin(59)
        picker.append(self._hour)
        picker.append(Gtk.Label(label=":", css_classes=["alarm-spin-separator"]))
        picker.append(self._minute)
        self._am_pm = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["linked"], valign=Gtk.Align.CENTER)
        self._am = Gtk.ToggleButton(label="AM")
        self._pm = Gtk.ToggleButton(label="PM", group=self._am)
        self._am_pm.append(self._am)
        self._am_pm.append(self._pm)
        picker.append(self._am_pm)
        edit.append(picker)
        enabled_row = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        enabled_row.append(Gtk.Label(label="Ring every day"))
        self._enabled = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        enabled_row.append(self._enabled)
        edit.append(enabled_row)
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        self._delete = Gtk.Button(label="Delete", css_classes=["pill", "destructive-action"])
        self._delete.connect("clicked", lambda *_: on_delete(self.selected_slot))
        self._save = Gtk.Button(label="Save", css_classes=["pill", "suggested-action"])
        self._save.connect("clicked", lambda *_: on_save(self.selected_slot, *self._values()))
        buttons.append(self._delete)
        buttons.append(self._save)
        edit.append(buttons)
        self._stack.add_named(edit, "edit")

    @property
    def selected_slot(self) -> int:
        return next((s for s, b in self._slot_buttons.items() if b.get_active()), SLOTS[0])

    def _slot_toggled(self, button: Gtk.ToggleButton, slot: int) -> None:
        if button.get_active() and not self._syncing:
            self._on_select(slot)

    def _select(self, slot: int) -> None:
        self._syncing = True
        try:
            self._slot_buttons[slot].set_active(True)
        finally:
            self._syncing = False

    def _values(self) -> tuple[int, int, bool]:
        hour = int(self._hour.get_value())
        if not self._clock_24h:
            hour = from_12h(hour, self._pm.get_active())
        return hour, int(self._minute.get_value()), self._enabled.get_active()

    def set_titles(self, subtitle: str) -> None:
        self._subtitle.set_label(subtitle)

    def set_busy(self, busy: bool) -> None:
        for widget in (self._save, self._delete):
            widget.set_sensitive(not busy)

    def show_loading(self, slot: int, message: str) -> None:
        self._select(slot)
        self._loading_label.set_label(message)
        self._stack.set_visible_child_name("loading")

    def show_error(self, message: str) -> None:
        self._error_label.set_label(message)
        self._stack.set_visible_child_name("error")

    def show_alarm(self, slot: int, alarm: Alarm | None, clock_24h: bool, now: dt.datetime) -> None:
        self._select(slot)
        self._clock_24h = clock_24h
        # A new alarm starts at the next full hour; an existing one shows its own time.
        hour, minute = (alarm.hour, alarm.minute) if alarm else ((now.hour + 1) % 24, 0)
        self._am_pm.set_visible(not clock_24h)
        if clock_24h:
            self._hour.set_range(0, 23)
            self._hour.set_value(hour)
        else:
            hour12, pm = to_12h(hour)
            self._hour.set_range(1, 12)
            self._hour.set_value(hour12)
            (self._pm if pm else self._am).set_active(True)
        self._minute.set_value(minute)
        self._enabled.set_active(alarm.enabled if alarm else True)
        if alarm is None:
            self._status.set_label(f"Alarm {slot} is not set yet")
        elif alarm.enabled:
            self._status.set_label(f"Rings every day at {format_time(alarm.hour, alarm.minute, clock_24h)}")
        else:
            self._status.set_label(f"Turned off (set for {format_time(alarm.hour, alarm.minute, clock_24h)})")
        self._save.set_label("Save" if alarm else "Create alarm")
        self._delete.set_visible(alarm is not None)
        self.set_busy(False)
        self._stack.set_visible_child_name("edit")


class AlarmRinging(Gtk.Box):
    """Shown in the calendar's place while alarms ring: Snooze or Dismiss."""

    def __init__(self, on_snooze: Callable[[], None], on_dismiss: Callable[[], None]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16, valign=Gtk.Align.FILL,
                         css_classes=["card", "calendar-panel", "alarm-ringing"])
        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, vexpand=True, valign=Gtk.Align.CENTER)
        self._icon = Gtk.Image(pixel_size=72, css_classes=["accent"])
        self._title = Gtk.Label(css_classes=["calendar-title"], wrap=True, justify=Gtk.Justification.CENTER)
        self._time = Gtk.Label(css_classes=["alarm-ringing-time"])
        center.append(self._icon)
        center.append(self._title)
        center.append(self._time)
        self.append(center)
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.snooze_button = Gtk.Button(label="Snooze 10 minutes", css_classes=["pill", "alarm-ringing-button"])
        self.snooze_button.connect("clicked", lambda *_: on_snooze())
        dismiss = Gtk.Button(label="Dismiss", css_classes=["pill", "suggested-action", "alarm-ringing-button"])
        dismiss.connect("clicked", lambda *_: on_dismiss())
        buttons.append(self.snooze_button)
        buttons.append(dismiss)
        self.append(buttons)
        self.connect("realize", lambda *_: self._icon.set_from_icon_name(alarm_icon(self)))

    def show(self, slots: list[int], now: dt.datetime, clock_24h: bool) -> None:
        names = [f"Alarm {s}" for s in slots]
        self._title.set_label(names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1])
        self._time.set_label(format_time(now.hour, now.minute, clock_24h))
        self._icon.set_from_icon_name(alarm_icon(self))
        GLib.idle_add(lambda: self.snooze_button.grab_focus() and False)
