"""GTK 4 / libadwaita user interface."""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import logging
import logging.handlers
import signal
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import (APP_ID, APP_NAME, __version__, audio, calendar_view, captions_view, changelog,  # noqa: E402
               alarms, alarms_view, backgrounds, compat, intercom, muting, speech, texts_view, themes,
               tts, weather)
from .config import Config, state_dir  # noqa: E402
from .intercom import CONNECTING, ENDED, FAILED, FOREIGN, HUNG_UP, LIVE, OFFLINE, REFUSED, SELF  # noqa: E402

log = logging.getLogger("linuxcomm")

WEATHER_REFRESH_S = 15 * 60
MAX_TALK_S = 5 * 60          # safety net against a forgotten open microphone
REPLY_WINDOW_S = 20          # how long "X was talking – Reply" stays up after a call
ALL = "all"

CSS = """
.clock-card, .weather-card { padding: 24px 28px; }
.clock-button { padding: 0; font-weight: normal; }  /* don't inherit bold button text */
.clock-time { font-size: 60pt; font-weight: 300; font-feature-settings: "tnum"; }
.narrow .clock-time { font-size: 34pt; }
.narrow .clock-card, .narrow .weather-card { padding: 18px 16px; }
.clock-date { font-size: 15pt; }
.weather-temp { font-size: 40pt; font-weight: 300; }
.narrow .weather-temp { font-size: 30pt; }
.weather-summary { font-size: 14pt; }
.weather-emoji { font-size: 56pt; }
.forecast-emoji { font-size: 20pt; }
.status-dot { min-width: 10px; min-height: 10px; border-radius: 999px;
              background-color: alpha(@window_fg_color, 0.25); }
.status-dot.online { background-color: @success_color; }
.status-dot.dnd { background-color: @warning_color; }
.status-dot.foreign, .status-dot.failed, .status-dot.refused { background-color: @error_color; }
.status-dot.live, .status-dot.incoming, .status-dot.connecting { background-color: @accent_color; }
.talk-button { min-height: 48px; font-size: 13pt; }
/* "Stop talking" is the same solid red in every theme and libadwaita version
   (newer libadwaita would otherwise draw a pressed red toggle button pale pink). */
button.hangup, button.hangup:checked { background-color: #E01B24; background-image: none; color: #FFFFFF; }
button.hangup:hover, button.hangup:checked:hover { background-color: #C7162B; }
button.hangup:active, button.hangup:checked:active { background-color: #A51D2D; }
.color-swatch { min-width: 14px; min-height: 14px; border-radius: 999px;
                box-shadow: 0 0 0 1px rgba(128, 128, 128, 0.7); }
.color-swatch.black { background-color: #000000; }
.color-swatch.white { background-color: #ffffff; }
"""


def local_address() -> str:
    """The LAN address other stations would use to reach this one."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # TEST-NET address: selects a route, sends nothing
            return s.getsockname()[0]
    except OSError:
        return socket.gethostname()


def _on_background(group: Adw.PreferencesGroup) -> None:
    """Give a group's title and description, which sit directly on the window background, the
    text color setting (call before adding rows, so nothing inside the list is included)."""
    texts = {group.get_title(), group.get_description()} - {"", None}
    widgets = [group]
    while widgets:
        widget = widgets.pop()
        if isinstance(widget, Gtk.Label) and widget.get_label() in texts:
            widget.add_css_class("on-background")
        child = widget.get_first_child()
        while child:
            widgets.append(child)
            child = child.get_next_sibling()


def _names(names) -> str:
    """"Kitchen", "Kitchen and Office", "Kitchen, Office and Garage" (each name once)."""
    names = list(dict.fromkeys(names))
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _style_talk_button(button: Gtk.Widget, content: Adw.ButtonContent, mode: str, idle_label: str,
                       idle_classes: tuple[str, ...] = ()) -> None:
    """A Talk button: "idle" (idle_label), "stop" (solid red while talking) or "send" (while
    recording a message for the synthetic voice, which pressing it again sends)."""
    for css in ("suggested-action", "destructive-action", "hangup"):
        button.remove_css_class(css)
    classes = {"stop": ("destructive-action", "hangup"), "send": ("suggested-action",)}.get(mode, idle_classes)
    for css in classes:
        button.add_css_class(css)
    content.set_icon_name("mail-send-symbolic" if mode == "send" else "audio-input-microphone-symbolic")
    content.set_label("Send" if mode == "send" else idle_label)


def _talking_to_you(calls) -> str:
    callers = list(dict.fromkeys(c.caller for c in calls))
    return f"{_names(callers)} {'is' if len(callers) == 1 else 'are'} talking to you. Press Reply to answer."


def _host_of(address: str) -> str:
    try:
        return intercom.parse_address(address)[0]
    except ValueError:
        return address


@dataclass
class ActiveTalk:
    id: str
    target: str                  # ALL, a peer id, or "addr:<ip>" for an unlisted caller
    label: str
    session: intercom.TalkSession | None      # None while recording a synthetic-voice message
    capture: audio.Capture
    targets: list[tuple[str, str]] = field(default_factory=list)   # (peer id, address)
    started: float = field(default_factory=time.monotonic)
    states: dict[str, tuple[str, str]] = field(default_factory=dict)
    message: tts.VoiceMessage | None = None   # recording for the synthetic voice
    heard: str = ""                           # what speech to text has recognized so far

    @property
    def target_ids(self) -> list[str]:
        return [tid for tid, _ in self.targets]


@dataclass
class OutgoingMessage:
    """A synthetic-voice message being prepared and sent after Talk was pressed again."""
    label: str
    text: str
    step: str = "Recognizing the last words for {}"


@dataclass
class IncomingText:
    caller: str
    address: str
    text: str
    received: GLib.DateTime


@dataclass
class TextTarget:
    """Who the composer sends to."""
    label: str
    address: str


class Dictation:
    """Speech to text into the composer: the microphone goes to a recognizer of its own.

    on_result(dictation, text, final) and on_error(dictation, message) run on the main loop.
    """

    def __init__(self, app: LinuxCommApp, target: TextTarget, on_result, on_error):
        self.target = target         # the composer it types into
        self.stopping = False        # the microphone is off; the last words are still being recognized
        engine = app.speech
        self.transcriber = engine.start_call(
            f"dictation-{uuid.uuid4().hex}", app.config.station_name, english=speech.is_english(engine.model_path),
            on_result=lambda _id, _who, text, final: GLib.idle_add(on_result, self, text, final))
        if self.transcriber is None:
            raise RuntimeError("speech to text is not available")
        try:
            self.capture = audio.Capture(app.devices.make_element("source", app.config.get("input_device")),
                                         on_data=self.transcriber.feed,
                                         on_error=lambda message: on_error(self, message))
            self.capture.start()
        except Exception:
            self.transcriber.finish()
            raise

    def stop(self) -> None:
        if not self.stopping:
            self.stopping = True
            self.capture.stop()
            self.transcriber.finish()


class _PlayAndTranscribe:
    """Sends incoming audio to the speaker and to speech recognition."""

    def __init__(self, player: audio.Player, transcriber: speech.CallTranscriber):
        self._player = player
        self._transcriber = transcriber

    def write(self, pcm: bytes) -> None:
        self._player.write(pcm)
        self._transcriber.feed(pcm)

    def close(self) -> None:
        try:
            self._player.close()
        finally:
            self._transcriber.finish()


class Station:
    """Connects the intercom server (running on worker threads) to audio and the UI."""

    def __init__(self, app: LinuxCommApp):
        self.app = app
        self.instance_id = uuid.uuid4().hex

    def station_name(self) -> str:
        return self.app.config.station_name

    def network_key(self) -> str:
        return self.app.config.get("network_key") or ""

    def do_not_disturb(self) -> bool:
        return bool(self.app.config.get("do_not_disturb"))

    def incoming_started(self, call: intercom.IncomingCall) -> intercom.AudioSink:
        cfg = self.app.config
        device = cfg.get("output_device")
        volume = cfg.get("incoming_volume") / 100
        player = audio.Player(self.app.devices.make_element("sink", device), volume)
        if cfg.get("chime"):
            audio.play_pcm_async(self.app.devices.make_element("sink", device), audio.CHIME, volume)
        self.app.muter.acquire()
        transcriber = self.app.start_transcriber(call)
        GLib.idle_add(self.app.to_window, "on_incoming_started", call)
        return player if transcriber is None else _PlayAndTranscribe(player, transcriber)

    def incoming_finished(self, call: intercom.IncomingCall) -> None:
        self.app.muter.release()
        GLib.idle_add(self.app.to_window, "on_incoming_finished", call)

    # Another station reading or changing our alarms (on a server thread).
    def alarms(self) -> dict:
        return self.app.alarms.to_json()

    def set_alarm(self, slot: int, time_text: str, enabled: bool, caller: str) -> dict:
        hour, minute = alarms.parse_time(time_text)
        alarm = self.app.alarms.set(slot, hour, minute, enabled)
        GLib.idle_add(self.app.to_window, "on_alarm_changed_remotely", slot, caller, alarm)
        return self.app.alarms.to_json()

    def delete_alarm(self, slot: int, caller: str) -> dict:
        self.app.alarms.delete(slot)
        GLib.idle_add(self.app.to_window, "on_alarm_changed_remotely", slot, caller, None)
        return self.app.alarms.to_json()

    def receive_text(self, caller: str, address: str, text: str) -> None:
        cfg = self.app.config
        if cfg.get("chime"):
            try:
                audio.play_pcm_async(self.app.devices.make_element("sink", cfg.get("output_device")),
                                     audio.CHIME, cfg.get("incoming_volume") / 100)
            except Exception as e:  # the text is still shown
                log.warning("Could not play the chime: %s", e)
        GLib.idle_add(self.app.to_window, "on_text_received",
                      IncomingText(caller, address, text, GLib.DateTime.new_now_local()))


class WeatherIcon(Gtk.Stack):
    """A symbolic weather icon, or an emoji if the icon theme lacks it."""

    def __init__(self, pixel_size: int, emoji_class: str):
        super().__init__()
        self._image = Gtk.Image(pixel_size=pixel_size)
        self._emoji = Gtk.Label(css_classes=[emoji_class])
        self.add_named(self._image, "icon")
        self.add_named(self._emoji, "emoji")

    def show(self, icon_name: str, emoji: str) -> None:
        if Gtk.IconTheme.get_for_display(self.get_display()).has_icon(icon_name):
            self._image.set_from_icon_name(icon_name)
            self.set_visible_child_name("icon")
        else:
            self._emoji.set_label(emoji)
            self.set_visible_child_name("emoji")


class PeerRow(Adw.ActionRow):
    def __init__(self, peer_id: str):
        super().__init__(activatable=True, use_markup=False)
        self.peer_id = peer_id
        self.dot = Gtk.Box(css_classes=["status-dot"], valign=Gtk.Align.CENTER)
        self.add_prefix(self.dot)
        self.alarm_button = Gtk.Button(icon_name=alarms_view.ALARM_ICONS[0], valign=Gtk.Align.CENTER,
                                       tooltip_text="Alarms on this station", css_classes=["flat", "circular"])
        self.alarm_button.connect("realize", lambda b: b.set_icon_name(alarms_view.alarm_icon(b)))
        self.add_suffix(self.alarm_button)
        self.talk_button = Gtk.ToggleButton(valign=Gtk.Align.CENTER, tooltip_text="Talk to this station")
        self.talk_button.set_child(Adw.ButtonContent(icon_name="audio-input-microphone-symbolic", label="Talk"))
        self.add_suffix(self.talk_button)
        self.text_button = Gtk.Button(valign=Gtk.Align.CENTER, tooltip_text="Send a text message to this station")
        self.text_button.set_child(Adw.ButtonContent(icon_name="mail-message-new-symbolic", label="Text"))
        self.add_suffix(self.text_button)

    def show(self, title: str, subtitle: str, dot_state: str) -> None:
        self.set_title(title)
        self.set_subtitle(subtitle)
        self.dot.set_css_classes(["status-dot", dot_state])


class CallBar(Gtk.Revealer):
    """The banner under the header while someone talks to this station: Hang Up and Reply."""

    def __init__(self, on_hang_up, on_reply):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        box = Gtk.Box(spacing=12, css_classes=["lc-banner", "call-bar"])
        self.title = Gtk.Label(hexpand=True, wrap=True, justify=Gtk.Justification.CENTER, css_classes=["heading"])
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icon = "call-stop-symbolic" if theme.has_icon("call-stop-symbolic") else "process-stop-symbolic"
        self.hang_up_button = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=["hangup"],
                                         tooltip_text="End the call: stop playing it and tell the caller")
        self.hang_up_button.set_child(Adw.ButtonContent(icon_name=icon, label="Hang Up"))
        self.hang_up_button.connect("clicked", lambda *_: on_hang_up())
        self.reply_button = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=["suggested-action"],
                                       tooltip_text="End the call and talk back")
        self.reply_button.set_child(Adw.ButtonContent(icon_name="audio-input-microphone-symbolic", label="Reply"))
        self.reply_button.connect("clicked", lambda *_: on_reply())
        for widget in (self.title, self.hang_up_button, self.reply_button):
            box.append(widget)
        self.set_child(box)

    def show(self, title: str, hang_up: bool, reply: bool) -> None:
        self.title.set_label(title)
        self.hang_up_button.set_visible(hang_up)
        self.reply_button.set_visible(reply)
        self.set_reveal_child(True)

    def hide(self) -> None:
        self.set_reveal_child(False)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: LinuxCommApp):
        super().__init__(application=app, title=APP_NAME, default_width=1120, default_height=760)
        self.set_size_request(360, 500)
        self.add_css_class("main-window")  # where the background image goes
        self.app = app
        self.config = app.config
        self._rows: dict[str, PeerRow] = {}
        self._peer_status: dict[str, intercom.PeerStatus] = {}
        self._incoming: dict[str, intercom.IncomingCall] = {}
        self._last_caller: intercom.IncomingCall | None = None
        self._last_caller_ended = 0.0
        self._offered_add: set[str] = set()
        self._talk: ActiveTalk | None = None
        self._mic_test: audio.Capture | None = None
        self._level = 0.0
        self._shown_level = 0.0
        self._level_timer = 0
        self._syncing = False
        self._inputs = [audio.DEFAULT_DEVICE]
        self._outputs = [audio.DEFAULT_DEVICE]
        self._weather_gen = 0
        self._have_weather = False
        self._ip_location: weather.Location | None = None
        self._last_clock = ("", "")
        self.transcript = speech.Transcript()
        self._caption_style = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self._caption_style,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self._now = dt.datetime.now          # the alarms' clock (tests replace it)
        self._alarm_peer: dict | None = None  # whose alarms the editor shows; None = this station
        self._remote_alarms: dict = {}
        self._ringing: list[int] = []
        self._ring_started: dt.datetime | None = None
        self._alarm_sound: audio.AlarmSound | None = None
        self._alarm_cells_key = None
        self._texts: list[IncomingText] = []           # incoming texts not closed yet; [0] is shown
        self._text_target: TextTarget | None = None    # the open composer's recipient
        self._dictation: Dictation | None = None
        self._told_voice_fallback = False
        self._outgoing: list[OutgoingMessage] = []
        self._send_after_dictation = False

        self._build_ui()
        self.apply_caption_style()
        self.refresh_captions()
        self.refresh_alarm_cells()
        self._rebuild_peers()
        self._populate_devices()
        self._tick()
        GLib.timeout_add(250, self._tick)
        self.refresh_weather()
        GLib.timeout_add_seconds(WEATHER_REFRESH_S, self.refresh_weather)

    # -- layout ----------------------------------------------------------------

    def _build_ui(self) -> None:
        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title=APP_NAME)
        header.set_title_widget(self.window_title)

        self.dnd_button = Gtk.ToggleButton(icon_name="notifications-disabled-symbolic",
                                           tooltip_text="Do not disturb: refuse incoming calls and texts",
                                           active=bool(self.config.get("do_not_disturb")))
        self.dnd_button.connect("toggled", self._on_dnd_toggled)
        header.pack_start(self.dnd_button)

        menu = Gio.Menu()
        menu.append("Preferences", "app.preferences")
        menu.append("Fullscreen", "app.fullscreen")
        menu.append(f"About {APP_NAME}", "app.about")
        menu.append("Quit", "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       primary=True, tooltip_text="Main menu"))

        self.server_banner = compat.banner()
        self.call_banner = CallBar(on_hang_up=self.hang_up_incoming, on_reply=self.reply)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        left.append(self._build_clock())
        left.append(self._build_weather())
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        right.append(self._build_intercom())
        right.append(self._build_audio())
        self.columns = Gtk.Box(spacing=24, homogeneous=True, margin_top=24, margin_bottom=24,
                               margin_start=18, margin_end=18)
        self.columns.append(self._build_left_panels(left))
        self.columns.append(right)

        clamp = Adw.Clamp(maximum_size=1200, tightening_threshold=1000, child=self.columns)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp, vexpand=True)
        self._scroller = scroller
        content = compat.watch_width(self, scroller, 820, self._set_narrow)
        self.captions = captions_view.CaptionsPanel()
        self.captions.connect("clicked", lambda *_: self.toggle_transcript())
        view = compat.toolbar_view([header, self.server_banner, self.call_banner], content, [self.captions])
        # Texts cover the whole window, header included; toasts stay above them.
        self.text_modal = texts_view.TextModal(
            on_close_message=self.close_text, on_reply=self.reply_to_text, on_send=self.send_text,
            on_cancel=self.close_composer, on_dictate=self._on_dictate_toggled, max_length=intercom.MAX_TEXT)
        self._view = view
        layers = Gtk.Overlay(child=view)
        layers.add_overlay(self.text_modal)
        self.toasts = Adw.ToastOverlay(child=layers)
        self.set_content(self.toasts)
        self.update_title()

    def _set_narrow(self, narrow: bool) -> None:
        self.columns.set_orientation(Gtk.Orientation.VERTICAL if narrow else Gtk.Orientation.HORIZONTAL)
        self.columns.set_homogeneous(not narrow)
        (self.add_css_class if narrow else self.remove_css_class)("narrow")
        self.text_modal.set_narrow(narrow)

    def _build_clock(self) -> Gtk.Widget:
        # The card holds Alarm 1–3 and, below them, the time, date and 7-day strip as one button,
        # so a click, a touchscreen tap or the keyboard opens the calendar.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.time_label = Gtk.Label(css_classes=["clock-time"], xalign=0)
        self.date_label = Gtk.Label(css_classes=["clock-date", "dim-label"], xalign=0, wrap=True)
        self.week_strip = calendar_view.WeekStrip()
        box.append(self.time_label)
        box.append(self.date_label)
        box.append(self.week_strip)
        clock_button = Gtk.Button(child=box, css_classes=["flat", "clock-button"], tooltip_text="Show the calendar")
        clock_button.connect("clicked", lambda *_: self.open_calendar())
        self.alarm_cells = alarms_view.AlarmCells(on_open=lambda slot: self.open_alarm_editor(slot))
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["card", "clock-card"])
        card.append(self.alarm_cells)
        card.append(clock_button)
        return card

    def _build_left_panels(self, covered: Gtk.Widget) -> Gtk.Widget:
        """The month calendar, the transcript and the alarm panels share one place over `covered`
        (the clock and weather), hidden until opened; the intercom on the right stays usable."""
        self.calendar = calendar_view.MonthCalendar(on_close=self.close_panel)
        self.transcript_panel = captions_view.TranscriptPanel(
            on_close=self.close_panel, on_save=self.save_transcript,
            on_copy=self._copy_transcript, on_clear=self._clear_transcript)
        self.alarm_editor = alarms_view.AlarmEditor(
            on_close=self.close_panel, on_select=self._alarm_slot_selected, on_save=self._save_alarm,
            on_delete=self._delete_alarm, on_retry=self._load_remote_alarms)
        self.alarm_ringing = alarms_view.AlarmRinging(on_snooze=self.snooze_alarms, on_dismiss=self.dismiss_alarms)
        self._panels = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._panels.add_named(self.calendar, "calendar")
        self._panels.add_named(self.transcript_panel, "transcript")
        self._panels.add_named(self.alarm_editor, "alarm")
        self._panels.add_named(self.alarm_ringing, "ringing")
        self.panel_revealer = Gtk.Revealer(child=self._panels, visible=False,
                                           transition_type=Gtk.RevealerTransitionType.CROSSFADE)
        self.panel_revealer.set_overflow(Gtk.Overflow.VISIBLE)  # don't clip the card's shadow
        self._covered = covered

        def on_revealed(revealer, _pspec):
            shown = revealer.get_child_revealed() and revealer.get_reveal_child()
            covered.set_opacity(0 if shown else 1)  # avoid the covered cards' edges showing around it
            # Once faded out, hide it completely so clicks reach the clock again.
            revealer.set_visible(revealer.get_reveal_child() or revealer.get_child_revealed())

        self.panel_revealer.connect("notify::child-revealed", on_revealed)
        overlay = Gtk.Overlay(child=covered)
        overlay.add_overlay(self.panel_revealer)
        overlay.set_measure_overlay(self.panel_revealer, True)  # never squeeze the panels

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)
        return overlay

    def _open_panel(self, name: str, focus: Gtk.Widget) -> None:
        if self._ringing and name != "ringing":
            self.toast("Dismiss or snooze the alarm first")
            return
        if self.panel_revealer.get_reveal_child():
            self._panels.set_visible_child_name(name)  # crossfade from the other panel
        else:
            self._panels.set_visible_child_full(name, Gtk.StackTransitionType.NONE)
        self.panel_revealer.set_visible(True)
        self.panel_revealer.set_reveal_child(True)
        self._scroller.get_vadjustment().set_value(0)  # in the narrow layout the panel is at the top
        focus.grab_focus()

    def panel_showing(self, name: str) -> bool:
        return self.panel_revealer.get_reveal_child() and self._panels.get_visible_child_name() == name

    def close_panel(self) -> None:
        if not self._ringing:  # a ringing alarm stays until it is dismissed or snoozed
            self._hide_panel()

    def _hide_panel(self) -> None:
        self._covered.set_opacity(1)  # fade the panel out over the clock and weather
        self.panel_revealer.set_reveal_child(False)

    def open_calendar(self) -> None:
        self.calendar.show_today()
        self._open_panel("calendar", self.calendar.close_button)

    def _on_key_pressed(self, _controller, keyval, _keycode, _state) -> bool:
        if keyval == Gdk.KEY_Escape and self.text_modal.showing():
            self.close_text() if self.text_modal.showing("message") else self.close_composer()
            return True
        if keyval == Gdk.KEY_Escape and self.panel_revealer.get_reveal_child():
            self.close_panel()  # (not while an alarm rings)
            return True
        return False

    def _build_weather(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["card", "weather-card"])
        self.weather_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vhomogeneous=False)
        card.append(self.weather_stack)

        spinner = Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True)
        spinner.set_size_request(32, 32)
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER,
                          margin_top=24, margin_bottom=24)
        loading.append(spinner)
        loading.append(Gtk.Label(label="Getting the weather…", css_classes=["dim-label"]))
        self.weather_stack.add_named(loading, "loading")

        error = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER,
                        margin_top=24, margin_bottom=24)
        self.weather_error = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, css_classes=["dim-label"])
        retry = Gtk.Button(label="Try again", halign=Gtk.Align.CENTER, css_classes=["pill"])
        retry.connect("clicked", lambda *_: self.refresh_weather())
        error.append(Gtk.Image(icon_name="weather-severe-alert-symbolic", pixel_size=48, css_classes=["dim-label"]))
        error.append(self.weather_error)
        error.append(retry)
        self.weather_stack.add_named(error, "error")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        top = Gtk.Box(spacing=18)
        self.weather_icon = WeatherIcon(96, "weather-emoji")
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER, hexpand=True)
        self.temp_label = Gtk.Label(css_classes=["weather-temp"], xalign=0)
        self.summary_label = Gtk.Label(css_classes=["weather-summary"], xalign=0, wrap=True)
        self.details_label = Gtk.Label(css_classes=["dim-label"], xalign=0, wrap=True)
        for w in (self.temp_label, self.summary_label, self.details_label):
            info.append(w)
        top.append(self.weather_icon)
        top.append(info)
        self.forecast_box = Gtk.Box(homogeneous=True, spacing=6)
        self.location_label = Gtk.Label(css_classes=["dim-label", "caption"], xalign=0, wrap=True)
        content.append(top)
        content.append(self.forecast_box)
        content.append(self.location_label)
        self.weather_stack.add_named(content, "weather")
        self.weather_stack.set_visible_child_name("loading")
        return card

    def _build_intercom(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        group = Adw.PreferencesGroup(title="Intercom",
                                     description="Click Talk to speak to a station, and click it again to stop. "
                                                 "Text sends it a message.")
        _on_background(group)
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add a station",
                         css_classes=["flat"], valign=Gtk.Align.CENTER)
        add.connect("clicked", lambda *_: self._open_peer_dialog())
        group.set_header_suffix(add)
        self.peer_list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        self.peer_list.set_placeholder(Gtk.Label(
            label="No stations yet.\nClick + to add another LinuxComm machine by IP address or host name.",
            wrap=True, justify=Gtk.Justification.CENTER, css_classes=["dim-label"],
            margin_top=24, margin_bottom=24, margin_start=12, margin_end=12))
        self.peer_list.connect("row-activated", lambda _l, row: self._open_peer_dialog(self.config.find_peer(row.peer_id)))
        group.add(self.peer_list)
        box.append(group)

        self.talk_all_button = Gtk.ToggleButton(css_classes=["pill", "suggested-action", "talk-button"])
        self.talk_all_content = Adw.ButtonContent(icon_name="audio-input-microphone-symbolic",
                                                  label="Talk to all stations", halign=Gtk.Align.CENTER)
        self.talk_all_button.set_child(self.talk_all_content)
        self.talk_all_button.connect("toggled", self._on_talk_toggled, ALL)
        box.append(self.talk_all_button)

        self.talk_status = Gtk.Box(spacing=12, visible=False)
        self.talk_label = Gtk.Label(xalign=0, hexpand=True, css_classes=["heading", "on-background"],
                                    ellipsize=Pango.EllipsizeMode.END)
        self.talk_level = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER, hexpand=True)
        self.talk_status.append(self.talk_label)
        self.talk_status.append(self.talk_level)
        box.append(self.talk_status)
        return box

    def _build_audio(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title="Audio devices")
        _on_background(group)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Look for new devices",
                             css_classes=["flat"], valign=Gtk.Align.CENTER)
        refresh.connect("clicked", lambda *_: self._populate_devices())
        group.set_header_suffix(refresh)

        self.input_row = Adw.ComboRow(title="Microphone", use_subtitle=True)
        self.mic_level = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER, width_request=64)
        self.mic_test = Gtk.ToggleButton(label="Test", valign=Gtk.Align.CENTER,
                                         tooltip_text="Show the microphone level")
        self.mic_test.connect("toggled", self._on_mic_test_toggled)
        self.input_row.add_suffix(self.mic_level)
        self.input_row.add_suffix(self.mic_test)
        self.input_row.connect("notify::selected", self._on_device_selected, "input")

        self.output_row = Adw.ComboRow(title="Speaker", use_subtitle=True)
        test = Gtk.Button(label="Test", valign=Gtk.Align.CENTER, tooltip_text="Play the chime")
        test.connect("clicked", self._on_speaker_test)
        self.output_row.add_suffix(test)
        self.output_row.connect("notify::selected", self._on_device_selected, "output")

        group.add(self.input_row)
        group.add(self.output_row)
        return group

    # -- small helpers -----------------------------------------------------------

    def toast(self, message: str, button: str | None = None, on_click=None, timeout: int = 4) -> None:
        toast = compat.toast(message, timeout)
        if button:
            toast.set_button_label(button)
            toast.connect("button-clicked", lambda *_: on_click())
        self.toasts.add_toast(toast)

    def update_title(self) -> None:
        subtitle = f"{self.config.station_name} · {local_address()}"
        if self.config.get("do_not_disturb"):
            subtitle += " · Do not disturb"
        self.window_title.set_subtitle(subtitle)

    def show_server_error(self, error: OSError) -> None:
        port = self.app.server.port
        if isinstance(error, PermissionError):
            text = f"Other stations can't reach this one: no permission to use port {port}. Run install.sh once to allow it."
        elif error.errno == errno.EADDRINUSE:
            text = f"Other stations can't reach this one: port {port} is used by another program (a web server?)."
        else:
            text = f"Other stations can't reach this one: {error}"
        self.server_banner.set_title(text)
        self.server_banner.set_revealed(True)

    def _set_toggle(self, button: Gtk.ToggleButton, active: bool, destructive: bool = True) -> None:
        if button.get_active() != active:
            button.set_active(active)
        if destructive:
            for css in ("destructive-action", "hangup"):
                (button.add_css_class if active else button.remove_css_class)(css)

    # -- clock -------------------------------------------------------------------

    def _tick(self) -> bool:
        now = GLib.DateTime.new_now_local()
        seconds = self.config.get("clock_seconds")
        if self.config.get("clock_24h"):
            time_text = now.format("%H:%M:%S" if seconds else "%H:%M")
        else:
            time_text = now.format("%l:%M:%S" if seconds else "%l:%M").strip()
            am_pm = now.format("%p")  # empty in locales without AM/PM
            if am_pm:
                time_text += f"<span size='45%'> {GLib.markup_escape_text(am_pm)}</span>"
        date_text = now.format("%A, %B %-d, %Y")
        if (time_text, date_text) != self._last_clock:
            new_day = date_text != self._last_clock[1]
            self._last_clock = (time_text, date_text)
            self.time_label.set_markup(time_text)
            self.date_label.set_label(date_text)
            if new_day:  # midnight: move the strip and the calendar's "today" along
                self.week_strip.update()
                self.calendar.refresh()

        moment = self._now()
        due = self.app.alarm_clock.due(moment)
        if due:
            self.start_ringing(due, moment)
        elif self._ringing and moment - self._ring_started > alarms.RING_LIMIT:
            self.dismiss_alarms()
            self.toast(f"The alarm stopped after {alarms.RING_LIMIT.seconds // 60} minutes")
        cells_key = (moment.strftime("%Y-%m-%d %H:%M"), bool(self.config.get("clock_24h")))
        if cells_key != self._alarm_cells_key:  # e.g. a snooze ended, or the time format changed
            self._alarm_cells_key = cells_key
            self.refresh_alarm_cells()

        if self._talk:
            if time.monotonic() - self._talk.started > MAX_TALK_S:
                self.stop_talk()
                self.toast(f"Stopped talking after {MAX_TALK_S // 60} minutes")
            else:
                self._update_talk_label()
        if (self._last_caller and not self._incoming
                and time.monotonic() - self._last_caller_ended > REPLY_WINDOW_S):
            self._last_caller = None
            self._update_call_banner()
        return True

    # -- weather -----------------------------------------------------------------

    def refresh_weather(self) -> bool:
        self._weather_gen += 1
        if not self._have_weather:
            self.weather_stack.set_visible_child_name("loading")
        threading.Thread(target=self._weather_worker, daemon=True, name="weather",
                         args=(self._weather_gen, (self.config.get("location") or "").strip(),
                               self.config.get("units") == "fahrenheit")).start()
        return True

    def _weather_worker(self, gen: int, query: str, fahrenheit: bool) -> None:
        try:
            if query:
                cache = self.config.get("location_cache")
                if isinstance(cache, dict) and cache.get("query") == query:
                    loc = weather.Location(cache["name"], cache["latitude"], cache["longitude"])
                else:
                    loc = weather.geocode(query)
                    self.config.set("location_cache", {"query": query, "name": loc.name,
                                                       "latitude": loc.latitude, "longitude": loc.longitude})
            else:
                loc = self._ip_location or weather.locate_by_ip()
                self._ip_location = loc
            result = weather.fetch_weather(loc, fahrenheit)
        except Exception as e:
            log.warning("Weather update failed: %s", e)
            GLib.idle_add(self._show_weather_error, gen, weather.describe_error(e))
        else:
            GLib.idle_add(self._show_weather, gen, result)

    def _show_weather(self, gen: int, w: weather.Weather) -> None:
        if gen != self._weather_gen:
            return
        text, icon, emoji = weather.describe(w.code, w.is_day)
        self.weather_icon.show(icon, emoji)
        self.temp_label.set_label(f"{round(w.temperature)}{w.temp_unit}")
        self.summary_label.set_label(f"{text} · H {round(w.high)}° L {round(w.low)}°")
        self.details_label.set_label(f"Feels like {round(w.apparent)}° · Humidity {round(w.humidity)}% · "
                                     f"Wind {round(w.wind)} {w.wind_unit}")
        updated = GLib.DateTime.new_now_local().format("%H:%M" if self.config.get("clock_24h") else "%l:%M %p")
        self.location_label.set_label(f"{w.location} · updated {updated.strip()} · Weather data by Open-Meteo.com")
        while child := self.forecast_box.get_first_child():
            self.forecast_box.remove(child)
        for day in w.days[1:5]:
            self.forecast_box.append(self._forecast_day(day))
        self._have_weather = True
        self.weather_stack.set_visible_child_name("weather")

    def _forecast_day(self, day: weather.DayForecast) -> Gtk.Widget:
        text, icon_name, emoji = weather.describe(day.code)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, tooltip_text=text)
        name = "Tomorrow" if day.date == dt.date.today() + dt.timedelta(days=1) else day.date.strftime("%a")
        box.append(Gtk.Label(label=name, css_classes=["caption-heading"]))
        icon = WeatherIcon(32, "forecast-emoji")
        icon.show(icon_name, emoji)
        box.append(icon)
        box.append(Gtk.Label(label=f"{round(day.high)}° / {round(day.low)}°", css_classes=["caption"]))
        return box

    def _show_weather_error(self, gen: int, message: str) -> None:
        if gen != self._weather_gen:
            return
        if self._have_weather:
            self.toast(f"Weather update failed: {message}")
        else:
            self.weather_error.set_label(message)
            self.weather_stack.set_visible_child_name("error")

    # -- peers -------------------------------------------------------------------

    def _rebuild_peers(self) -> None:
        peers = self.config.peers()
        ids = {p["id"] for p in peers}
        for pid in [pid for pid in self._rows if pid not in ids]:
            self.peer_list.remove(self._rows.pop(pid))
            self._peer_status.pop(pid, None)
        for p in peers:
            if p["id"] not in self._rows:
                row = PeerRow(p["id"])
                row.talk_button.connect("toggled", self._on_talk_toggled, p["id"])
                row.alarm_button.connect("clicked", lambda *_, pid=p["id"]: self.open_alarm_editor(1, peer_id=pid))
                row.text_button.connect("clicked", lambda *_, pid=p["id"]: self.compose_text(pid))
                self.peer_list.append(row)
                self._rows[p["id"]] = row
        self._refresh_rows()
        self._sync_talk_ui()

    def on_peer_status(self, peer_id: str, status: intercom.PeerStatus) -> None:
        if peer_id in self._rows:
            self._peer_status[peer_id] = status
            self._refresh_rows()

    def _peer_for_call(self, call: intercom.IncomingCall) -> str | None:
        return self._find_peer(call.address, call.caller)

    def _find_peer(self, address: str, name: str) -> str | None:
        """The listed station at `address` (as seen by our server), or else the one called `name`."""
        peers = self.config.peers()
        for p in peers:
            status = self._peer_status.get(p["id"])
            if _host_of(p["address"]) == address or (status and status.ip == address):
                return p["id"]
        for p in peers:
            status = self._peer_status.get(p["id"])
            if p["name"].casefold() == name.casefold() or (status and status.name == name):
                return p["id"]
        return None

    def _refresh_rows(self) -> None:
        incoming = {self._peer_for_call(c) for c in self._live_incoming()}
        talk_targets = set(self._talk.target_ids) if self._talk else set()
        for p in self.config.peers():
            row = self._rows.get(p["id"])
            if row is None:
                continue
            status = self._peer_status.get(p["id"])
            dot = status.state if status else "unknown"
            if p["id"] in talk_targets and self._talk.message:
                activity, dot = "Recording a message for it", "live"
            elif p["id"] in talk_targets:
                state, detail = self._talk.states.get(p["id"], (CONNECTING, ""))
                activity = {CONNECTING: "Connecting…", LIVE: "Live: you are talking", ENDED: "Finished",
                            HUNG_UP: "Hung up"}.get(state, detail)
                dot = state
            elif p["id"] in incoming:
                activity, dot = "Talking to you", "incoming"
            elif status:
                activity = status.detail
                if status.name and status.name != p["name"]:
                    activity += f" as “{status.name}”"
            else:
                activity = "Checking…"
            row.show(p["name"], f"{p['address']} · {activity}", dot)

    def _open_peer_dialog(self, peer: dict | None = None) -> None:
        dialog = compat.alert_dialog(heading="Edit Station" if peer else "Add Station",
                                     body="Enter the IP address or host name of another computer running "
                                          "LinuxComm, or its URL if it is behind a reverse proxy.")
        group = Adw.PreferencesGroup()
        name_row = Adw.EntryRow(title="Name (e.g. Kitchen)", activates_default=True)
        address_row = Adw.EntryRow(title="IP address, host name or URL", activates_default=True)
        if peer:
            name_row.set_text(peer["name"])
            address_row.set_text(peer["address"])
        group.add(name_row)
        group.add(address_row)
        examples = f"For example 192.168.1.20, kitchen.local or http://example.com{intercom.BASE_PATH}"
        hint = Gtk.Label(label=examples, wrap=True, xalign=0, css_classes=["dim-label", "caption"])
        extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        extra.append(group)
        extra.append(hint)
        dialog.set_extra_child(extra)
        dialog.add_response("cancel", "Cancel")
        if peer:
            dialog.add_response("delete", "Remove")
            dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.add_response("save", "Save" if peer else "Add")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def validate(*_):
            text = address_row.get_text()
            try:
                intercom.parse_address(text)
                problem = None
            except ValueError as e:
                problem = str(e)
            dialog.set_response_enabled("save", problem is None)
            show_problem = bool(problem and text.strip())  # don't nag about an empty field
            hint.set_label(problem if show_problem else examples)
            for css, wanted in (("error", show_problem), ("dim-label", not show_problem)):
                (hint.add_css_class if wanted else hint.remove_css_class)(css)

        def on_response(_dialog, response):
            name, address = name_row.get_text().strip(), address_row.get_text().strip()
            if response == "save":
                if peer:
                    self.config.update_peer(peer["id"], name, address)
                    self._peer_status.pop(peer["id"], None)
                else:
                    self.config.add_peer(name, address)
            elif response == "delete":
                if self._talk and self._talk.target == peer["id"]:
                    self.stop_talk()
                self.config.remove_peer(peer["id"])
            else:
                return
            self._rebuild_peers()
            self.app.monitor.refresh()

        address_row.connect("changed", validate)
        validate()
        dialog.connect("response", on_response)
        self._dialog = dialog  # keep the wrapper alive while shown (see compat.watch_width)
        compat.present(dialog, self)

    # -- incoming calls ------------------------------------------------------------

    def on_incoming_started(self, call: intercom.IncomingCall) -> None:
        self._incoming[call.id] = call
        self._last_caller = call
        self._refresh_rows()
        self._sync_talk_ui()  # no new talks while someone talks to us (and the call bar)
        if self._peer_for_call(call) is None and call.address not in self._offered_add:
            self._offered_add.add(call.address)
            self.toast(f"{call.caller} ({call.address}) is not in your station list", button="Add",
                       on_click=lambda: self._add_caller(call), timeout=10)
        if not self.is_active():
            note = Gio.Notification.new(f"{call.caller} is talking")
            note.set_body("To all stations" if call.broadcast else "To you")
            note.set_priority(Gio.NotificationPriority.HIGH)
            self.app.send_notification(f"call-{call.address}", note)
        if self.app.speech_active():
            self.on_caption(call.id, call.caller, "…", False)  # "Kitchen: …" until words arrive

    def on_incoming_finished(self, call: intercom.IncomingCall) -> None:
        self._incoming.pop(call.id, None)
        if self._last_caller and self._last_caller.id == call.id:
            self._last_caller_ended = time.monotonic()
        self._refresh_rows()
        self._sync_talk_ui()

    def _live_incoming(self) -> list[intercom.IncomingCall]:
        """Calls still coming in (a call that was hung up ends a moment later)."""
        return [c for c in self._incoming.values() if not c.hung_up]

    def hang_up_incoming(self, reason: str = "hangup") -> None:
        """End every incoming talk. Their audio stops, and the callers see that this station hung up
        (or, with reason "reply", that it is replying)."""
        calls = self._live_incoming()
        for call in calls:
            call.hang_up(reason)
        if calls and reason == "hangup":
            if self._last_caller in calls:
                self._last_caller = None  # no "was talking · Reply" after hanging up
            self.toast(f"Hung up on {_names(c.caller for c in calls)}")
        self._refresh_rows()
        self._sync_talk_ui()

    # -- text messages ---------------------------------------------------------------

    def on_text_received(self, message: IncomingText) -> None:
        log.info("Text from %s (%s)", message.caller, message.address)
        self._texts.append(message)
        if self.text_modal.showing("compose"):
            self.toast(f"New text from {message.caller}", timeout=6)  # shown once the composer closes
        else:
            self._show_text()
        if (self._find_peer(message.address, message.caller) is None
                and message.address not in self._offered_add):
            self._offered_add.add(message.address)
            self.toast(f"{message.caller} ({message.address}) is not in your station list", button="Add",
                       on_click=lambda: self._add_station(message.caller, message.address), timeout=10)
        if not self.is_active():
            note = Gio.Notification.new(f"Text from {message.caller}")
            note.set_body(message.text)
            note.set_priority(Gio.NotificationPriority.HIGH)
            self.app.send_notification("text", note)
            self.present()  # bring LinuxComm to the front, where the desktop allows it

    def _show_text(self) -> None:
        """Show the oldest text not closed yet, or take the pop-up away when there is none."""
        if not self._texts:
            self._set_text_modal(False)
            self.app.withdraw_notification("text")
            return
        message = self._texts[0]
        when = texts_view.when_text(message.received, bool(self.config.get("clock_24h")))
        self._set_text_modal(True)
        self.text_modal.show_message(message.caller, message.text, when, len(self._texts) - 1)

    def _set_text_modal(self, shown: bool) -> None:
        # While a text covers the window, the keyboard and mouse can't reach what is underneath.
        self._view.set_can_focus(not shown)
        self._view.set_can_target(not shown)
        self.text_modal.set_visible(shown)

    def close_text(self) -> None:
        if self._texts:
            self._texts.pop(0)
        self._show_text()

    def reply_to_text(self) -> None:
        if not self._texts:
            return
        message = self._texts.pop(0)
        peer_id = self._find_peer(message.address, message.caller)
        peer = self.config.find_peer(peer_id) if peer_id else None
        target = TextTarget(peer["name"], peer["address"]) if peer else TextTarget(message.caller, message.address)
        self._open_composer(target, f"Reply to {target.label}")

    def compose_text(self, peer_id: str) -> None:
        peer = self.config.find_peer(peer_id)
        if peer:
            self._open_composer(TextTarget(peer["name"], peer["address"]), f"Text to {peer['name']}")

    def _open_composer(self, target: TextTarget, title: str) -> None:
        self._text_target = target
        self._send_after_dictation = False
        self._set_text_modal(True)
        self.text_modal.show_compose(title, self._dictation_problem())

    def close_composer(self) -> None:
        if self._dictation:
            self._dictation.stop()
        self._dictation = None
        self._text_target = None
        if not self.config.get("stt_enabled"):
            self.app.update_speech()  # unload a speech model loaded just for dictation
        self._show_text()  # a text that arrived meanwhile, if any

    def send_text(self, text: str) -> None:
        target = self._text_target
        if target is None:
            return
        self.text_modal.set_busy(True)
        if self._dictation is not None:
            self._send_after_dictation = True  # once the last spoken words are typed in
            self._stop_dictation()
            self.text_modal.set_partial("Finishing dictation…")
            return
        station, key = self.config.station_name, self.config.get("network_key") or ""

        def work():
            try:
                intercom.send_text(target.address, text, station, key)
                error = None
            except intercom.RemoteError as e:
                error = str(e)
            GLib.idle_add(done, error)

        def done(error):
            still_open = self._text_target is target
            if error:
                self.toast(f"{target.label}: {error}", timeout=8)
                if still_open:
                    self.text_modal.set_busy(False)  # keep the message to try again
            else:
                log.info("Sent a text to %s", target.label)
                self.toast(f"Text sent to {target.label}")
                if still_open:
                    self.close_composer()
            return False

        threading.Thread(target=work, name="send-text", daemon=True).start()

    def _dictation_problem(self) -> str | None:
        """Why Dictate can't be used, or None."""
        if not self.app.speech.available:
            return "Speech to text is not installed; run the installer again"
        if self.app.speech_model_path() is None:
            return "No speech model is installed; see Preferences → Captions"
        return None

    def _on_dictate_toggled(self, on: bool) -> None:
        if on:
            self._start_dictation()
        else:
            self._stop_dictation()

    def _start_dictation(self) -> None:
        engine = self.app.speech
        if self._text_target is None:
            return
        if engine.model_path is None:  # captions are off: load the model just for this
            engine.load(self.app.speech_model_path())
        if engine.error:
            self.text_modal.set_dictating(False)
            self.toast(f"Speech recognition failed: {engine.error}")
            return
        try:
            dictation = Dictation(self.app, self._text_target, self._on_dictation, self._on_dictation_error)
        except Exception as e:
            log.exception("Could not start dictation")
            self.text_modal.set_dictating(False)
            self.toast(f"Can't use the microphone: {e}")
            return
        # A previous dictation still finishing keeps typing its last words in.
        self._dictation = dictation
        self.text_modal.set_dictating(True)
        self.text_modal.set_partial("Loading speech recognition…" if engine.loading else "Listening…")

    def _stop_dictation(self) -> None:
        if self._dictation:
            self._dictation.stop()
        self.text_modal.set_dictating(False)

    def _on_dictation(self, dictation: Dictation, text: str, final: bool) -> bool:
        if dictation.target is not self._text_target:
            return False  # its composer was closed
        current = dictation is self._dictation
        if not final:
            if current and text:
                self.text_modal.set_partial(f"{text}…")
        elif text:
            self.text_modal.insert_words(text)
            if current and not dictation.stopping:
                self.text_modal.set_partial("Listening…")
        elif current:  # the end of the dictation
            self._dictation = None
            if self._send_after_dictation:
                self._send_after_dictation = False
                self.text_modal.set_partial("")
                self.text_modal.set_busy(False)
                if self.text_modal.text().strip():
                    self.send_text(self.text_modal.text())
            else:
                self.text_modal.set_partial("")
        return False

    def _on_dictation_error(self, dictation: Dictation, message: str) -> None:
        if dictation is self._dictation and not dictation.stopping:
            self._stop_dictation()
            self.toast(f"Microphone error: {message}")

    # -- alarms --------------------------------------------------------------------

    def refresh_alarm_cells(self) -> None:
        clock_24h = bool(self.config.get("clock_24h"))
        for slot, alarm in self.app.alarms.all().items():
            self.alarm_cells.update(slot, alarm, clock_24h, self.app.alarm_clock.snoozed.get(slot),
                                    slot in self._ringing)

    def _alarm_when(self, hour: int, minute: int) -> str:
        return alarms.format_time(hour, minute, bool(self.config.get("clock_24h")))

    def open_alarm_editor(self, slot: int, peer_id: str | None = None) -> None:
        """Show Alarm `slot` of this station, or of another station, in the left panel."""
        peer = self.config.find_peer(peer_id) if peer_id else None
        if peer_id and peer is None:
            return
        self._open_panel("alarm", self.alarm_editor.close_button)
        if not self.panel_showing("alarm"):
            return  # an alarm is ringing
        self._alarm_peer = peer
        self._remote_alarms = {}
        if peer is None:
            self.alarm_editor.set_titles("This station")
            self._show_alarm_slot(slot)
        else:
            self.alarm_editor.set_titles(f"{peer['name']} · {peer['address']}")
            self.alarm_editor.show_loading(slot, f"Getting the alarms of {peer['name']}…")
            self._load_remote_alarms()

    def _show_alarm_slot(self, slot: int) -> None:
        if self._alarm_peer is None:
            alarm = self.app.alarms.get(slot)
        else:
            try:
                alarm = alarms.alarm_from_json(self._remote_alarms.get(str(slot)))
            except (ValueError, TypeError):
                alarm = None
        self.alarm_editor.show_alarm(slot, alarm, bool(self.config.get("clock_24h")), self._now())

    def _alarm_slot_selected(self, slot: int) -> None:
        if self._alarm_peer is None or self._remote_alarms:
            self._show_alarm_slot(slot)

    def _remote_alarm_call(self, call, done: str | None = None) -> None:
        """Run a request to the station being edited; show its alarms, or close with `done`."""
        peer = self._alarm_peer

        def work():
            try:
                result, error = call(), None
            except intercom.RemoteError as e:
                result, error = None, str(e)
            GLib.idle_add(finish, result, error)

        def finish(result, error):
            if self._alarm_peer is not peer or not self.panel_showing("alarm"):
                return False  # the editor was closed or moved on meanwhile
            if error and not self._remote_alarms:
                self.alarm_editor.show_error(f"{peer['name']}: {error}")
            elif error:
                self.alarm_editor.set_busy(False)
                self.toast(f"{peer['name']}: {error}")
            else:
                self._remote_alarms = result
                if done:
                    self.close_panel()
                    self.toast(done)
                else:
                    self._show_alarm_slot(self.alarm_editor.selected_slot)
            return False

        threading.Thread(target=work, name="remote-alarms", daemon=True).start()

    def _load_remote_alarms(self) -> None:
        peer = self._alarm_peer
        if peer is None:
            return
        self._remote_alarms = {}
        self.alarm_editor.show_loading(self.alarm_editor.selected_slot, f"Getting the alarms of {peer['name']}…")
        station, key = self.config.station_name, self.config.get("network_key") or ""
        self._remote_alarm_call(lambda: intercom.fetch_alarms(peer["address"], station, key))

    def _save_alarm(self, slot: int, hour: int, minute: int, enabled: bool) -> None:
        when = self._alarm_when(hour, minute)
        summary = f"Alarm {slot} rings every day at {when}" if enabled else f"Alarm {slot} is set for {when} but off"
        peer = self._alarm_peer
        if peer is None:
            try:
                self.app.alarms.set(slot, hour, minute, enabled)
            except OSError as e:
                self.toast(f"Could not save the alarm: {e.strerror or e}")
                return
            self.app.alarm_clock.cancel(slot)  # a new time replaces a snooze
            self.refresh_alarm_cells()
            self.close_panel()
            self.toast(summary)
        else:
            self.alarm_editor.set_busy(True)
            station, key = self.config.station_name, self.config.get("network_key") or ""
            self._remote_alarm_call(
                lambda: intercom.set_remote_alarm(peer["address"], slot, f"{hour:02d}:{minute:02d}", enabled,
                                                  station, key),
                done=f"{peer['name']}: {summary}")

    def _delete_alarm(self, slot: int) -> None:
        peer = self._alarm_peer
        if peer is None:
            try:
                self.app.alarms.delete(slot)
            except OSError as e:
                self.toast(f"Could not delete the alarm: {e.strerror or e}")
                return
            self.app.alarm_clock.cancel(slot)
            self.refresh_alarm_cells()
            self.close_panel()
            self.toast(f"Alarm {slot} deleted")
        else:
            self.alarm_editor.set_busy(True)
            station, key = self.config.station_name, self.config.get("network_key") or ""
            self._remote_alarm_call(lambda: intercom.delete_remote_alarm(peer["address"], slot, station, key),
                                    done=f"{peer['name']}: Alarm {slot} deleted")

    def on_alarm_changed_remotely(self, slot: int, caller: str, alarm: alarms.Alarm | None) -> None:
        """Another station changed one of our alarms (from the server thread, via idle_add)."""
        self.app.alarm_clock.cancel(slot)
        self.refresh_alarm_cells()
        if self._alarm_peer is None and self.panel_showing("alarm") and self.alarm_editor.selected_slot == slot:
            self._show_alarm_slot(slot)
        if alarm is None:
            self.toast(f"{caller} deleted Alarm {slot}")
        else:
            state = "" if alarm.enabled else " (off)"
            self.toast(f"{caller} set Alarm {slot} to {self._alarm_when(alarm.hour, alarm.minute)}{state}")

    def start_ringing(self, slots: list[int], now: dt.datetime) -> None:
        new = [slot for slot in slots if slot not in self._ringing]
        if not new:
            return
        if not self._ringing:
            self._ring_started = now
        self._ringing = sorted(set(self._ringing) | set(new))
        log.info("Alarm %s ringing", ", ".join(map(str, self._ringing)))
        self.alarm_ringing.show(self._ringing, now, bool(self.config.get("clock_24h")))
        self._open_panel("ringing", self.alarm_ringing.snooze_button)
        if self._alarm_sound is None:
            device = self.config.get("output_device")
            self._alarm_sound = audio.AlarmSound(lambda: self.app.devices.make_element("sink", device))
        self.refresh_alarm_cells()
        if not self.is_active():
            note = Gio.Notification.new(" and ".join(f"Alarm {s}" for s in self._ringing))
            note.set_body(self._alarm_when(now.hour, now.minute))
            note.set_priority(Gio.NotificationPriority.URGENT)
            self.app.send_notification("alarm", note)

    def snooze_alarms(self) -> None:
        until = self.app.alarm_clock.snooze(self._ringing, self._now())
        self._stop_ringing()
        self.toast(f"Snoozed until {self._alarm_when(until.hour, until.minute)}")

    def dismiss_alarms(self) -> None:
        self._stop_ringing()

    def _stop_ringing(self) -> None:
        self._ringing = []
        self._ring_started = None
        if self._alarm_sound is not None:
            self._alarm_sound.stop()
            self._alarm_sound = None
        self.app.withdraw_notification("alarm")
        self._hide_panel()
        self.refresh_alarm_cells()

    # -- captions ------------------------------------------------------------------

    def apply_caption_style(self) -> None:
        compat.load_css(self._caption_style, captions_view.style_css(
            self.config.get("caption_font") or "", self.config.get("caption_size") or 18))

    def refresh_captions(self) -> None:
        engine = self.app.speech
        self.captions.set_visible(bool(self.config.get("stt_enabled")) and engine.available)
        items = self.transcript.recent(2)
        if items:
            self.captions.show_lines(items)
        elif engine.loading:
            self.captions.show_placeholder("Loading speech recognition…")
        elif engine.error:
            self.captions.show_placeholder(f"Speech recognition failed: {engine.error}")
        elif engine.model_path is None:
            self.captions.show_placeholder("No speech model is installed; see Preferences → Captions")
        else:
            self.captions.show_placeholder("Captions of what other stations say appear here")

    def on_caption(self, call_id: str, caller: str, text: str, final: bool) -> None:
        if final:
            new_session = self.transcript.final(call_id, caller, text)
        else:
            new_session = self.transcript.partial(call_id, caller, text)
        self.refresh_captions()
        # Keep an open transcript live: redraw it when a sentence is finished (or a new
        # conversation starts), otherwise just update the words still being spoken.
        self._render_transcript(live_only=not (final or new_session))

    def _format_time(self, timestamp: float) -> str:
        moment = GLib.DateTime.new_from_unix_local(int(timestamp))
        return moment.format("%H:%M:%S" if self.config.get("clock_24h") else "%l:%M:%S %p").strip()

    def toggle_transcript(self) -> None:
        if self.panel_showing("transcript"):
            self.close_panel()
        else:
            self.open_transcript()

    def open_transcript(self) -> None:
        self._open_panel("transcript", self.transcript_panel.close_button)
        self._render_transcript()

    def _render_transcript(self, live_only: bool = False) -> None:
        if not self.panel_showing("transcript"):
            return
        live = [speech.format_line(line, self._format_time) for line in self.transcript.live()]
        if live_only:
            self.transcript_panel.show_live(live)
            return
        finished = [speech.format_line(line, self._format_time) for line in self.transcript.lines]
        started = self.transcript.started
        who = list(dict.fromkeys(self.transcript.callers() + [line.caller for line in self.transcript.live()]))
        since = f"Since {self._format_time(started)}" if started and (finished or live) else ""
        self.transcript_panel.show(finished, live, " · ".join(part for part in (since, ", ".join(who)) if part))

    def _clear_transcript(self) -> None:
        self.transcript.clear()
        self._render_transcript()
        self.refresh_captions()

    def _copy_transcript(self) -> None:
        text = self.transcript.text(self._format_time)
        self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(text))
        self.toast("Transcript copied" if text else "The transcript is empty")

    def transcript_folder(self) -> str:
        """Where Save writes transcripts: the chosen folder, or ~/linuxcomm/data."""
        chosen = (self.config.get("transcript_folder") or "").strip()
        return str(Path(chosen).expanduser()) if chosen else str(speech.default_transcript_folder())

    def save_transcript(self) -> None:
        folder = self.transcript_folder()
        try:
            path = speech.save_transcript(self.transcript, folder, self.config.station_name)
        except ValueError as e:
            self.toast(str(e))
        except PermissionError:
            self.toast(f"No permission to save in {folder}. Choose another folder in Preferences → Captions.",
                       timeout=8)
        except OSError as e:
            self.toast(f"Could not save the transcript: {e.strerror or e}", timeout=8)
        else:
            log.info("Saved transcript to %s", path)
            self.toast(f"Saved {path}", timeout=6)

    def _add_caller(self, call: intercom.IncomingCall) -> None:
        self._add_station(call.caller, call.address)

    def _add_station(self, name: str, address: str) -> None:
        self.config.add_peer(name, address)
        self._rebuild_peers()
        self.app.monitor.refresh()

    def _reply_target(self, call: intercom.IncomingCall) -> str:
        return self._peer_for_call(call) or f"addr:{call.address}"

    def _update_call_banner(self) -> None:
        live = self._live_incoming()
        if live:
            callers = {c.caller for c in live}
            title = f"{_names(c.caller for c in live)} {'is' if len(callers) == 1 else 'are'} talking"
            if all(c.broadcast for c in live):
                title += " to everyone"
        elif self._last_caller:
            title = f"{self._last_caller.caller} was talking"
        else:
            self.call_banner.hide()
            return
        replying = bool(self._talk and self._last_caller
                        and self._talk.target == self._reply_target(self._last_caller))
        self.call_banner.show(title, hang_up=bool(live), reply=not replying)

    def reply(self) -> None:
        """Answer the last caller: end every talk, incoming and outgoing, then talk to them."""
        call = self._last_caller
        if not call:
            return
        target = self._reply_target(call)
        self.hang_up_incoming("reply")  # the callers see "… is replying"
        if target.startswith("addr:"):
            self.start_talk(target, address=call.address, label=call.caller)
        else:
            self.start_talk(target)

    # -- talking -------------------------------------------------------------------

    def _on_talk_toggled(self, button: Gtk.ToggleButton, target: str) -> None:
        if self._syncing:
            return
        if button.get_active():
            self.start_talk(target)
        elif self._talk and self._talk.target == target:
            self.stop_talk()

    def start_talk(self, target: str, address: str | None = None, label: str | None = None) -> None:
        if self._talk and self._talk.target == target:
            return
        live = self._live_incoming()
        if live:
            # Talking back while someone talks to us would feed their voice back to them.
            self.toast(_talking_to_you(live))
            self._sync_talk_ui()
            return
        self.stop_talk(sync=False)
        self._stop_mic_test()
        peers = self.config.peers()
        if target == ALL:
            # Skip stations known to be unreachable; unknown ones are tried anyway.
            skip = (SELF, OFFLINE, FOREIGN)
            targets = [(p["id"], p["address"]) for p in peers
                       if getattr(self._peer_status.get(p["id"]), "state", "") not in skip]
            label = "all stations"
            if peers and not targets:
                self.toast("No other stations are reachable right now")
                self._sync_talk_ui()
                return
        elif address:
            targets = [(target, address)]
        else:
            peer = next((p for p in peers if p["id"] == target), None)
            targets = [(peer["id"], peer["address"])] if peer else []
            label = peer["name"] if peer else ""
        if not targets:
            self.toast("Add a station first: click + next to Intercom")
            self._sync_talk_ui()
            return

        talk_id = uuid.uuid4().hex
        synthetic = bool(self.config.get("tts_relay"))
        if synthetic and (problem := self._tts_problem()):
            self.toast(f"Can't talk with the synthetic voice: {problem}")
            self._sync_talk_ui()
            return
        if synthetic and (error := self.app.tts.start_errors.get("coqui")) and not self._told_voice_fallback:
            self._told_voice_fallback = True
            self.toast(f"The natural voices didn't start ({error}); using an eSpeak voice", timeout=10)
        echo = message = session = None
        try:
            if synthetic:
                # Only record: the microphone goes to speech to text, and nothing is sent until
                # Talk is pressed again (see _send_voice_message).
                message = self._voice_message(talk_id)
            else:
                session = intercom.TalkSession(
                    targets, self.config.station_name, self.config.get("network_key") or "",
                    broadcast=target == ALL,
                    on_state=lambda tid, state, detail: GLib.idle_add(self._on_talk_state, talk_id, tid, state,
                                                                      detail))
            echo = self._echo_canceller()
            capture = audio.Capture(self.app.devices.make_element("source", self.config.get("input_device")),
                                    on_data=message.microphone if message else session.feed,
                                    on_level=self._set_level_from_thread,
                                    on_error=self._on_capture_error, feedback=self._feedback_suppressor(),
                                    echo=echo)
            capture.start()
        except Exception as e:
            if echo:
                echo.stop()
            if message:
                message.transcriber.finish()
            log.exception("Could not start talking")
            self.toast(f"Can't use the microphone: {e}")
            self._sync_talk_ui()
            return
        if session:
            session.start()
        self._talk = ActiveTalk(talk_id, target, label or address or "", session, capture, targets, message=message)
        self._start_level_timer()
        self._refresh_rows()
        self._sync_talk_ui()

    def _feedback_suppressor(self) -> audio.FeedbackSuppressor | None:
        """Acoustic feedback suppression for a talk; it is always in place so the setting also
        works mid-talk."""
        try:
            return audio.FeedbackSuppressor(enabled=bool(self.config.get("feedback_suppression")))
        except RuntimeError as e:
            log.warning("Acoustic feedback suppression is not available: %s", e)
            return None

    def _echo_canceller(self) -> audio.EchoCanceller | None:
        """Echo cancellation for a talk, already recording the speaker's sound, or None."""
        if not self.config.get("echo_cancellation") or not audio.EchoCanceller.available():
            return None
        label = self.config.get("output_device_name") or "the speaker"
        try:
            echo = audio.EchoCanceller(self.app.devices.make_monitor(self.config.get("output_device")), label)
            echo.start()
        except Exception as e:  # talking works without it
            log.warning("Echo cancellation is not available: %s", e)
            return None
        log.info("Echo cancellation on: removing the sound of %s from the microphone (%s)", label,
                 "measuring the delay for WebRTC 0.3" if echo.measures_delay else "WebRTC finds the delay")
        return echo

    def set_feedback_suppression(self, on: bool) -> None:
        self.config.set("feedback_suppression", on)
        log.info("Acoustic feedback suppression %s", "on" if on else "off")
        feedback = self._talk.capture.feedback if self._talk else None
        if feedback:
            feedback.set_enabled(on)
            self._update_talk_label()

    def _tts_problem(self) -> str | None:
        """Why talking with a synthetic voice isn't possible right now, or None."""
        if not self.app.speech.available or self.app.speech_model_path() is None:
            return "speech to text isn't installed; run the installer again"
        if not self.app.tts.voices():
            return ("no text-to-speech voices are installed; run the installer again" if self.app.tts.checked
                    else "the voices are still being prepared; try again in a moment")
        return None

    def _voice_message(self, talk_id: str) -> tts.VoiceMessage:
        """Recording a message for the synthetic voice (see tts.VoiceMessage)."""
        engine = self.app.speech
        if engine.model_path is None:
            engine.load(self.app.speech_model_path())

        def start_transcriber(on_result):
            return engine.start_call(f"voice-{talk_id}", self.config.station_name,
                                     english=speech.is_english(engine.model_path), on_result=on_result)

        return tts.VoiceMessage(start_transcriber,
                                on_heard=lambda text: GLib.idle_add(self._on_voice_heard, talk_id, text))

    def _on_voice_heard(self, talk_id: str, text: str) -> bool:
        if self._talk and self._talk.id == talk_id:
            self._talk.heard = text
            self._update_talk_label()
        return GLib.SOURCE_REMOVE

    def _send_voice_message(self, talk: ActiveTalk) -> None:
        """After recording: have everything recognized spoken once by the synthetic voice and send
        it as one message, in the background (the status line follows it)."""
        outgoing = OutgoingMessage(talk.label, talk.heard)
        self._outgoing.append(outgoing)
        voices, key = self.app.tts, self.config.get("tts_voice")
        station, network_key = self.config.station_name, self.config.get("network_key") or ""

        def show(step: str, text: str | None = None) -> None:
            def update():
                outgoing.step = step
                if text is not None:
                    outgoing.text = text
                self._update_talk_label()
                return GLib.SOURCE_REMOVE
            GLib.idle_add(update)

        def work():
            result = None
            try:
                text = talk.message.finish()  # waits for the last words
                if not text:
                    result = "Nothing was recognized, so nothing was sent"
                    return
                show("Preparing the voice for {}", text)
                voice = voices.pick(key)
                ready = voices.prepare(voice) if voice else None
                if ready is None:
                    result = "No synthetic voice could be started, so nothing was sent"
                    return
                if ready != voice:
                    GLib.idle_add(self.toast, f"{voice.label} didn't start; using {ready.label}")
                pcm = voices.say(ready, text)
                show("Sending to {}")
                result = self._deliver(talk, pcm, text, station, network_key)
            except Exception as e:
                log.exception("Could not send the voice message")
                result = f"The message wasn't sent: {e}"
            finally:
                GLib.idle_add(done, result)

        def done(result):
            self._outgoing.remove(outgoing)
            self._sync_talk_ui()
            if result:
                self.toast(result, timeout=8)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=work, name="voice-message", daemon=True).start()

    def _deliver(self, talk: ActiveTalk, pcm: bytes, text: str, station: str, network_key: str) -> str | None:
        """Send one spoken message to the talk's stations, like a talk (on a worker thread).
        Returns a confirmation to show, or None if nobody got it (refusals are shown anyway)."""
        states: dict[str, str] = {}

        def on_state(tid, state, detail):
            states[tid] = state
            GLib.idle_add(self._on_talk_state, talk.id, tid, state, detail)  # shows refusals, hang-ups

        session = intercom.TalkSession(talk.targets, station, network_key, broadcast=talk.target == ALL,
                                       on_state=on_state)
        session.start()
        gone = (REFUSED, FAILED, HUNG_UP, ENDED)
        # Wait until the stations answer, so the start of the message isn't sent in a burst.
        deadline = time.monotonic() + 2 * intercom.CONNECT_TIMEOUT_S
        while time.monotonic() < deadline and not all(states.get(t) in (LIVE, *gone) for t in talk.target_ids):
            time.sleep(0.05)
        if any(states.get(t) == LIVE for t in talk.target_ids):
            tts.stream(pcm + b"\0" * 9600, session.feed,  # and 0.3 s of silence
                       should_stop=lambda: all(states.get(t) in gone for t in talk.target_ids))
        session.stop()
        session.join(15)
        if not any(states.get(t) == ENDED for t in talk.target_ids):
            return None
        return f"Sent to {talk.label}: “{text}”"

    def set_tts_relay(self, on: bool) -> None:
        self.config.set("tts_relay", on)
        log.info("Talking with a synthetic voice %s", "on" if on else "off")
        self.app.update_speech()  # its speech to text needs the model even with captions off
        if on:
            self.app.prepare_voice()
        elif not (self._talk and self._talk.message) and not self._outgoing:
            self.app.tts.stop()  # frees Coqui's memory

    def stop_talk(self, sync: bool = True) -> None:
        talk, self._talk = self._talk, None
        if talk:
            talk.capture.stop()
            if talk.message:
                self._send_voice_message(talk)  # now the recording is spoken and sent
            else:
                talk.session.stop()
        if sync:
            self._refresh_rows()
            self._sync_talk_ui()

    def _on_talk_state(self, talk_id: str, target_id: str, state: str, detail: str) -> None:
        talk = self._talk
        name = next((p["name"] for p in self.config.peers() if p["id"] == target_id), None)
        who = name or (talk.label if talk else target_id)
        if state in (REFUSED, FAILED):
            self.toast(f"{who}: {detail}")
        elif state == HUNG_UP:
            self.toast(f"{who} is replying" if detail == "reply" else f"{who} hung up")
        if talk is None or talk.id != talk_id:
            return
        talk.states[target_id] = (state, detail)
        done = [s for s, _ in talk.states.values() if s in (REFUSED, FAILED, ENDED, HUNG_UP)]
        if len(done) == len(talk.target_ids):
            self.stop_talk()  # nobody is listening any more
            return
        self._refresh_rows()
        self._update_talk_label()

    def _on_capture_error(self, message: str) -> None:
        if self._talk:
            self.stop_talk()
        self._stop_mic_test()
        self.toast(f"Microphone error: {message}")

    def _sync_talk_ui(self) -> None:
        talk = self._talk
        recording = bool(talk and talk.message)  # a synthetic-voice message: pressing again sends it
        self._syncing = True
        try:
            talking_all = bool(talk and talk.target == ALL)
            self._set_toggle(self.talk_all_button, talking_all, destructive=False)
            for pid, row in self._rows.items():
                self._set_toggle(row.talk_button, bool(talk and talk.target == pid), destructive=False)
            self._set_toggle(self.mic_test, self._mic_test is not None, destructive=False)
        finally:
            self._syncing = False
        # While someone talks to us, only Reply (or stopping our own talk) is possible.
        live = self._live_incoming()
        blocked = _talking_to_you(live) if live else ""
        for pid, row in self._rows.items():
            talking_here = bool(talk and talk.target == pid)
            # "Talk"; red while talking; "Send" while recording a message for the synthetic voice
            _style_talk_button(row.talk_button, row.talk_button.get_child(),
                               ("send" if recording else "stop") if talking_here else "idle", "Talk")
            row.talk_button.set_sensitive(talking_here or not blocked)
            row.talk_button.set_tooltip_text(
                blocked if blocked and not talking_here else
                "Send the recorded message" if talking_here and recording else "Talk to this station")
        self.talk_all_button.set_sensitive(talking_all or (bool(self.config.peers()) and not blocked))
        self.talk_all_button.set_tooltip_text(blocked if blocked and not talking_all else None)
        _style_talk_button(self.talk_all_button, self.talk_all_content,
                           ("send" if recording else "stop") if talking_all else "idle", "Talk to all stations",
                           idle_classes=("suggested-action",))
        if talking_all:
            self.talk_all_content.set_label("Send to all stations" if recording else "Stop talking")
        self.talk_status.set_visible(talk is not None or bool(self._outgoing))
        self._update_talk_label()
        self._update_call_banner()

    def _update_talk_label(self) -> None:
        talk = self._talk
        if not talk:
            if self._outgoing:  # a synthetic-voice message on its way
                o = self._outgoing[-1]
                text = o.step.format(o.label)
                self.talk_label.set_label(f"{text}: “{o.text}”" if o.text else f"{text}…")
            return
        elapsed = int(time.monotonic() - talk.started)
        clock = f"{elapsed // 60}:{elapsed % 60:02d}"
        if talk.message:
            who = "all stations" if talk.target == ALL else talk.label
            text = f"Recording a message for {who} · {clock}"
            self.talk_label.set_label(f"{text} · “{talk.heard}”" if talk.heard else text)
            return
        total = len(talk.target_ids)
        live = sum(1 for s, _ in talk.states.values() if s == LIVE)
        if talk.target == ALL:
            text = f"Talking to all stations ({live} of {total} listening) · {clock}"
        elif live:
            text = f"Talking to {talk.label} · {clock}"
        else:
            text = f"Connecting to {talk.label}…"
        feedback = talk.capture.feedback
        if feedback and feedback.notches:
            text += " · Feedback suppressed"
        self.talk_label.set_label(text)

    # -- audio devices and levels --------------------------------------------------

    def _populate_devices(self) -> None:
        try:
            inputs, outputs = self.app.devices.refresh()
        except Exception:
            log.exception("Could not list audio devices")
            inputs, outputs = [audio.DEFAULT_DEVICE], [audio.DEFAULT_DEVICE]
        self._inputs = self._fill_combo(self.input_row, inputs, "input_device")
        self._outputs = self._fill_combo(self.output_row, outputs, "output_device")

    def _fill_combo(self, row: Adw.ComboRow, devices: list[audio.AudioDevice], key: str) -> list[audio.AudioDevice]:
        saved = self.config.get(key)
        devices = list(devices)
        index = next((i for i, d in enumerate(devices) if d.id == saved), None)
        if index is None:
            index = 0
            if saved:  # keep showing the chosen device while it is unplugged
                devices.append(audio.AudioDevice(saved, f"{self.config.get(key + '_name') or saved} (not connected)"))
                index = len(devices) - 1
        self._syncing = True
        try:
            row.set_model(Gtk.StringList.new([d.name for d in devices]))
            row.set_selected(index)
        finally:
            self._syncing = False
        return devices

    def _on_device_selected(self, row: Adw.ComboRow, _pspec, which: str) -> None:
        if self._syncing:
            return
        devices = self._inputs if which == "input" else self._outputs
        index = row.get_selected()
        if index >= len(devices):
            return
        self.config.set(f"{which}_device", devices[index].id)
        self.config.set(f"{which}_device_name", devices[index].name)
        if which == "input" and self._mic_test:
            self._stop_mic_test()
            self._start_mic_test()

    def _on_speaker_test(self, _button) -> None:
        try:
            sink = self.app.devices.make_element("sink", self.config.get("output_device"))
            audio.play_pcm_async(sink, audio.CHIME, self.config.get("incoming_volume") / 100)
        except Exception as e:
            self.toast(f"Can't use the speaker: {e}")

    def _on_mic_test_toggled(self, button: Gtk.ToggleButton) -> None:
        if self._syncing:
            return
        if button.get_active():
            if self._talk:
                self.toast("The level is already shown while talking")
                self._sync_talk_ui()
            else:
                self._start_mic_test()
        else:
            self._stop_mic_test()

    def _start_mic_test(self) -> None:
        try:
            capture = audio.Capture(self.app.devices.make_element("source", self.config.get("input_device")),
                                    on_level=self._set_level_from_thread, on_error=self._on_capture_error)
            capture.start()
        except Exception as e:
            self.toast(f"Can't use the microphone: {e}")
            capture = None
        self._mic_test = capture
        if capture:
            self._start_level_timer()
        self._sync_talk_ui()

    def _stop_mic_test(self) -> None:
        if self._mic_test:
            self._mic_test.stop()
            self._mic_test = None
            self._sync_talk_ui()

    def _set_level_from_thread(self, level: float) -> None:
        self._level = max(self._level, level)  # peak since the meter last updated

    def _start_level_timer(self) -> None:
        if not self._level_timer:
            self._level_timer = GLib.timeout_add(50, self._update_level)

    def _update_level(self) -> bool:
        active = self._talk is not None or self._mic_test is not None
        level, self._level = self._level, 0.0
        self._shown_level = max(level, self._shown_level - 0.06) if active else 0.0
        self.mic_level.set_value(self._shown_level)
        self.talk_level.set_value(self._shown_level)
        if not active:
            self._level_timer = 0
        return active

    # -- misc ---------------------------------------------------------------------

    def _on_dnd_toggled(self, button: Gtk.ToggleButton) -> None:
        self.config.set("do_not_disturb", button.get_active())
        self.update_title()
        self.toast("Do not disturb is on: incoming calls and texts are refused" if button.get_active()
                   else "Do not disturb is off")

    def toggle_talk_all(self) -> None:
        if self.talk_all_button.get_sensitive():
            self.talk_all_button.set_active(not self.talk_all_button.get_active())

    def _build_caption_preferences(self) -> Adw.PreferencesGroup:
        cfg, engine = self.config, self.app.speech
        group = Adw.PreferencesGroup(
            title="Captions",
            description="Speech to text for what other stations say, shown at the bottom of the window. "
                        "It runs entirely on this computer, and transcripts are never saved.")
        settings_rows: list[Gtk.Widget] = []

        def set_enabled(on: bool) -> None:
            cfg.set("stt_enabled", on)
            self.app.update_speech()
            for row in settings_rows:
                row.set_sensitive(on)

        if engine.available:
            group.add(compat.switch_row("Speech to text", bool(cfg.get("stt_enabled")), set_enabled,
                                        subtitle="Show captions of incoming speech"))
            models = speech.find_models()
            if models:
                model_row = Adw.ComboRow(title="Language model",
                                         model=Gtk.StringList.new([speech.model_label(p) for p in models]))
                current = self.app.speech_model_path()
                model_row.set_selected(models.index(current) if current in models else 0)

                def choose_model(row, _pspec):
                    cfg.set("stt_model", str(models[row.get_selected()]))
                    self.app.update_speech()

                model_row.connect("notify::selected", choose_model)
            else:
                model_row = Adw.ActionRow(title="Language model",
                                          subtitle="None installed. Run the installer again, or see the README.")
            settings_rows.append(model_row)
        else:
            group.add(Adw.ActionRow(title="Speech to text",
                                    subtitle="Not installed. Run the installer again to add it."))

        def set_font(family: str) -> None:
            cfg.set("caption_font", family)
            self.apply_caption_style()

        def set_size(size: float) -> None:
            cfg.set("caption_size", int(size))
            self.apply_caption_style()

        font_row = Adw.ActionRow(title="Font")
        font_row.add_suffix(compat.font_family_button(cfg.get("caption_font") or "", set_font))
        settings_rows.append(font_row)
        settings_rows.append(compat.spin_row("Size", "Points", 8, 72, 1, cfg.get("caption_size") or 18, set_size))
        folder_row = Adw.EntryRow(title="Save transcripts to", show_apply_button=True,
                                  text=self.transcript_folder())

        def set_folder(row) -> None:
            text = row.get_text().strip()
            default = speech.default_transcript_folder()
            # Keep "" (the default) unless another folder was chosen, so it follows the home folder.
            cfg.set("transcript_folder", "" if not text or Path(text).expanduser() == default else text)
            row.set_text(self.transcript_folder())

        folder_row.connect("apply", set_folder)
        settings_rows.append(folder_row)
        for row in settings_rows:
            row.set_sensitive(engine.available and bool(cfg.get("stt_enabled")))
            group.add(row)
        return group

    def _add_voice_rows(self, group: Adw.PreferencesGroup, dialog) -> None:
        """Talking with a synthetic voice: the switch, the voice, and a button to hear it."""
        cfg, voices = self.config, self.app.tts
        speech_ok = self.app.speech.available and self.app.speech_model_path() is not None
        about = ("Talk records what you say and turns it into text on this computer; when you press Talk "
                 "again, the voice below reads it out and that is sent as one message. Your microphone's "
                 "sound is never sent, so there is no echo or feedback." if speech_ok else
                 "Needs speech to text, which isn't installed. Run the installer again.")
        switch = compat.switch_row("Text-to-speech voice (TTS)", bool(cfg.get("tts_relay")) and speech_ok,
                                   self.set_tts_relay, subtitle=about)
        switch.set_sensitive(speech_ok)
        group.add(switch)

        row = Adw.ComboRow(title="Voice", use_subtitle=True)  # the chosen voice's full name as the subtitle
        row.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        if hasattr(row, "set_enable_search"):  # libadwaita 1.4+: type to find a voice
            row.set_enable_search(True)
            if row.find_property("search-match-mode") is not None:  # anywhere in the name, not just its start
                row.set_property("search-match-mode", Gtk.StringFilterMatchMode.SUBSTRING)

        # The drop-down shows each voice's full name (the default cuts them to the row's width),
        # with a check mark on the chosen one.
        shown: set[Gtk.ListItem] = set()

        def mark(item: Gtk.ListItem) -> None:
            chosen = item.get_item() is not None and item.get_item() is row.get_selected_item()
            item.get_child().get_last_child().set_opacity(1 if chosen else 0)

        def setup_item(_factory, item: Gtk.ListItem) -> None:
            box = Gtk.Box(spacing=12)
            box.append(Gtk.Label(xalign=0, hexpand=True))
            box.append(Gtk.Image(icon_name="object-select-symbolic"))
            item.set_child(box)

        def bind_item(_factory, item: Gtk.ListItem) -> None:
            item.get_child().get_first_child().set_label(item.get_item().get_string())
            shown.add(item)
            mark(item)

        items = Gtk.SignalListItemFactory()
        items.connect("setup", setup_item)
        items.connect("bind", bind_item)
        items.connect("unbind", lambda _factory, item: shown.discard(item))
        row.set_list_factory(items)
        row.connect("notify::selected-item", lambda *_: [mark(item) for item in list(shown)])
        play = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Hear this voice",
                          valign=Gtk.Align.CENTER, css_classes=["flat"])
        row.add_suffix(play)
        group.add(row)
        choices: list[tts.Voice] = []
        filling = [False]

        def fill() -> bool:
            if row.get_root() is None and choices:
                return GLib.SOURCE_REMOVE  # Preferences was closed
            choices[:] = voices.voices()
            filling[0] = True
            try:
                if not choices:
                    row.set_model(Gtk.StringList.new(["No voices are installed" if voices.checked
                                                      else "Preparing the voices…"]))
                else:
                    chosen = voices.pick(cfg.get("tts_voice"))
                    row.set_model(Gtk.StringList.new([v.label for v in choices]))
                    row.set_selected(choices.index(chosen) if chosen in choices else 0)
            finally:
                filling[0] = False
            natural = any(v.engine == "coqui" for v in choices)
            switch.set_subtitle(about + ("" if natural or not choices or not speech_ok else
                                         " Only eSpeak voices: Coqui TTS's natural voices aren't installed or "
                                         "didn't start."))
            row.set_sensitive(bool(choices))
            play.set_sensitive(bool(choices))
            if not voices.checked or not choices:  # look again while they are being prepared
                GLib.timeout_add_seconds(1, lambda: row.get_root() is not None and fill())
            return GLib.SOURCE_REMOVE

        def choose(_row, _pspec) -> None:
            index = row.get_selected()
            if filling[0] or index >= len(choices):
                return
            cfg.set("tts_voice", choices[index].key)
            if cfg.get("tts_relay"):
                self.app.prepare_voice()

        def hear(_button) -> None:
            voice = voices.pick(cfg.get("tts_voice"))
            if voice is None:
                return
            play.set_sensitive(False)
            dialog.add_toast(compat.toast(f"Preparing {voice.label}…", 3))

            def work():
                try:
                    ready = voices.prepare(voice)  # slow the first time for natural voices
                    if ready is None:
                        raise RuntimeError(voices.errors.get(voice.engine) or "no voice could be started")
                    pcm = voices.say(ready, "Hello! This is how I will sound on the intercom.")
                    sink = self.app.devices.make_element("sink", cfg.get("output_device"))
                    audio.play_pcm_async(sink, pcm, cfg.get("incoming_volume") / 100)
                    message = None if ready == voice else f"{voice.label} didn't start; this is {ready.label}"
                except Exception as e:
                    message = f"The voice didn't work: {e}"
                GLib.idle_add(done, message)

            def done(message):
                play.set_sensitive(True)
                if message:
                    dialog.add_toast(compat.toast(message, 6))
                return GLib.SOURCE_REMOVE

            threading.Thread(target=work, name="tts-preview", daemon=True).start()

        row.connect("notify::selected", choose)
        play.connect("clicked", hear)
        fill()

    def _add_text_color_rows(self, group: Adw.PreferencesGroup):
        """Text color: Black and White buttons, and any color as HTML hex or from a color picker.

        Returns a function to call after the theme changes.
        """
        cfg = self.config
        syncing = [False]
        row = Adw.ActionRow(title="Text color")
        presets = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
        preset_buttons: dict[str, Gtk.ToggleButton] = {}
        for name, value in themes.TEXT_COLORS.items():
            content = Gtk.Box(spacing=6)
            content.append(Gtk.Box(css_classes=["color-swatch", name.lower()], valign=Gtk.Align.CENTER))
            content.append(Gtk.Label(label=name))
            button = Gtk.ToggleButton(child=content, tooltip_text=value)
            # Clicking the chosen one again keeps it chosen.
            button.connect("toggled", lambda b, v=value: syncing[0] or (
                set_color(v) if b.get_active() else show(cfg.get("text_color"))))
            presets.append(button)
            preset_buttons[value] = button
        row.add_suffix(presets)
        group.add(row)

        hex_row = Adw.EntryRow(title="Text color as hex, e.g. #1a2b3c", show_apply_button=True)
        picker, show_in_picker = compat.color_button(cfg.get("text_color"), "Text Color", lambda c: set_color(c))
        hex_row.add_suffix(picker)
        group.add(hex_row)

        def show(color: str) -> None:
            syncing[0] = True
            try:
                for value, button in preset_buttons.items():
                    button.set_active(value == color)
                hex_row.set_text(color)
                hex_row.remove_css_class("error")
                show_in_picker(color)
            finally:
                syncing[0] = False
            problem = themes.text_color_problem(color, themes.get(cfg.get("theme")))
            row.set_subtitle(problem or "The Intercom and Audio devices headings and other text "
                                        "directly on the window background")

        def set_color(color: str) -> None:
            cfg.set("text_color", color)
            show(color)
            self.app.apply_theme()

        def apply_hex(entry) -> None:
            color = themes.parse_hex(entry.get_text())
            if color is None:
                entry.add_css_class("error")  # stays until it is fixed
            else:
                set_color(color)

        def theme_changed() -> None:
            # Black or white text that would be hard to read on the new theme switches to the other one.
            color, theme = cfg.get("text_color"), themes.get(cfg.get("theme"))
            if color in themes.TEXT_COLORS.values() and themes.text_color_problem(color, theme):
                color = max(themes.TEXT_COLORS.values(), key=lambda c: themes.contrast(c, theme.background))
                cfg.set("text_color", color)
            show(color)

        hex_row.connect("apply", apply_hex)
        show(cfg.get("text_color"))
        return theme_changed

    def _add_background_rows(self, group: Adw.PreferencesGroup, dialog) -> None:
        """Background image rows: a file name or path, a URL to download, and the image's strength."""
        cfg = self.config
        group.set_description(f"Background images are kept in {backgrounds.images_folder()}.")

        def toast(message: str) -> None:
            dialog.add_toast(compat.toast(message, 5))  # the window's own toasts would be hidden behind it

        image_row = Adw.EntryRow(title="Background image (file name or path)", show_apply_button=True,
                                 text=cfg.get("background_image") or "")
        image_row.connect("apply", lambda row: self._set_background(row.get_text(), image_row, toast))
        remove = Gtk.Button(icon_name="edit-clear-symbolic", tooltip_text="No background image",
                            css_classes=["flat"], valign=Gtk.Align.CENTER)
        remove.connect("clicked", lambda *_: self._set_background("", image_row, toast))
        image_row.add_suffix(remove)
        group.add(image_row)

        url_row = Adw.EntryRow(title="Download an image from a URL")
        download = Gtk.Button(label="Download", valign=Gtk.Align.CENTER)
        start = lambda *_: self._download_background(url_row, download, image_row, toast)  # noqa: E731
        download.connect("clicked", start)
        url_row.connect("entry-activated", start)
        url_row.add_suffix(download)
        group.add(url_row)

        def set_strength(value: float) -> None:
            cfg.set("background_strength", int(value))
            self.app.apply_theme()

        group.add(compat.spin_row("Image strength", "Percent; lower keeps text easier to read over busy images",
                                  10, 100, 5, cfg.get("background_strength"), set_strength))

    def _set_background(self, setting: str, image_row: Adw.EntryRow, toast, announce: bool = True) -> bool:
        """Use an image (a name in ~/linuxcomm/data/images or a path; "" = none) if it can be shown."""
        setting = setting.strip()
        if setting:
            path = backgrounds.resolve(setting)
            if not path.is_file():
                toast(f"No image at {path}")
                return False
            try:
                Gdk.Texture.new_from_filename(str(path))  # can GTK show it?
            except GLib.Error:
                toast(f"{path.name} isn't an image LinuxComm can show")
                return False
            setting = backgrounds.setting_for(path)
        self.config.set("background_image", setting)
        image_row.set_text(setting)
        self.app.apply_theme()
        if announce:
            toast(f"Background: {setting}" if setting else "No background image")
        return True

    def _download_background(self, url_row: Adw.EntryRow, button: Gtk.Button, image_row: Adw.EntryRow,
                             toast) -> None:
        url = url_row.get_text().strip()
        if not url or not button.get_sensitive():
            return
        button.set_sensitive(False)
        button.set_label("Downloading…")

        def work():
            try:
                path, error = backgrounds.download(url), None
                log.info("Downloaded background image from %s to %s", url, path)
            except Exception as e:
                log.info("Background download from %s failed: %s", url, e)
                path, error = None, backgrounds.describe_error(e)
            GLib.idle_add(done, path, error)

        def done(path, error):
            button.set_sensitive(True)
            button.set_label("Download")
            if error:
                toast(error)
            elif self._set_background(backgrounds.setting_for(path), image_row, toast, announce=False):
                url_row.set_text("")
                toast(f"Downloaded {path.name} and set it as the background")
            else:
                path.unlink(missing_ok=True)  # not an image GTK can show
            return False

        threading.Thread(target=work, name="background-download", daemon=True).start()

    def open_preferences(self) -> None:
        cfg = self.config
        dialog = compat.preferences_dialog("Preferences")
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        dialog.add(page)

        appearance = Adw.PreferencesGroup(title="Appearance")
        theme_keys = list(themes.THEMES)
        theme_row = Adw.ComboRow(title="Theme", subtitle="Changes apply right away",
                                 model=Gtk.StringList.new([themes.THEMES[k].name for k in theme_keys]))
        theme_row.set_selected(theme_keys.index(cfg.get("theme")) if cfg.get("theme") in theme_keys else 0)

        appearance.add(theme_row)
        text_color_theme_changed = self._add_text_color_rows(appearance)

        def choose_theme(row, _pspec):
            cfg.set("theme", theme_keys[row.get_selected()])
            text_color_theme_changed()
            self.app.apply_theme()

        theme_row.connect("notify::selected", choose_theme)
        self._add_background_rows(appearance, dialog)
        page.add(appearance)

        station = Adw.PreferencesGroup(
            title="This Station",
            description=f"Other stations can add this one as {local_address()} or {socket.gethostname()}.")
        name_row = Adw.EntryRow(title="Station name", text=cfg.station_name, show_apply_button=True)

        def apply_name(row):
            cfg.set("station_name", row.get_text().strip())
            self.update_title()

        name_row.connect("apply", apply_name)
        station.add(name_row)
        page.add(station)

        security = Adw.PreferencesGroup(
            title="Network Key",
            description="When set, stations only accept calls from stations that use the same key. "
                        "Leave it empty to accept calls from anyone on your network.")
        key_row = Adw.PasswordEntryRow(title="Network key", text=cfg.get("network_key") or "", show_apply_button=True)
        key_row.connect("apply", lambda row: cfg.set("network_key", row.get_text()))
        security.add(key_row)
        page.add(security)

        talking = Adw.PreferencesGroup(title="Talking")
        talking.add(compat.switch_row(
            "Acoustic feedback suppression", bool(cfg.get("feedback_suppression")), self.set_feedback_suppression,
            subtitle="Stops howling in your microphone's sound, e.g. when another station's speaker is close "
                     "enough to pick up your voice again. Speech is left unchanged."))
        echo_ok = audio.EchoCanceller.available()
        echo_row = compat.switch_row(
            "Echo cancellation", bool(cfg.get("echo_cancellation")) and echo_ok,
            lambda on: cfg.set("echo_cancellation", on),
            subtitle="Removes what this computer's speaker plays (other stations, the chime, music) from your "
                     "microphone while you talk. Takes effect from the next talk." if echo_ok else
                     "Not available: needs GStreamer's WebRTC audio processing (gstreamer1.0-plugins-bad, which "
                     "Ubuntu 24.04 doesn't include). Run the installer again.")
        echo_row.set_sensitive(echo_ok)
        talking.add(echo_row)
        self._add_voice_rows(talking, dialog)
        page.add(talking)

        incoming = Adw.PreferencesGroup(title="Incoming Calls")
        incoming.add(compat.switch_row("Chime", bool(cfg.get("chime")), lambda on: cfg.set("chime", on),
                                       subtitle="Play a ding-dong when someone starts talking"))
        incoming.add(compat.switch_row("Mute other apps", bool(cfg.get("mute_other_apps")),
                                       lambda on: cfg.set("mute_other_apps", on),
                                       subtitle="Silence music and videos on this computer while someone "
                                                "is talking to you"))
        incoming.add(compat.spin_row("Volume", "Percent of normal loudness", 0, 200, 10, cfg.get("incoming_volume"),
                                     lambda value: cfg.set("incoming_volume", int(value))))
        page.add(incoming)

        page.add(self._build_caption_preferences())

        clock = Adw.PreferencesGroup(title="Clock")
        time_format = Adw.ComboRow(title="Time format",
                                   model=Gtk.StringList.new(["24-hour (14:30)", "12-hour (2:30 PM)"]))
        time_format.set_selected(0 if cfg.get("clock_24h") else 1)
        time_format.connect("notify::selected", lambda row, _p: cfg.set("clock_24h", row.get_selected() == 0))
        clock.add(time_format)
        clock.add(compat.switch_row("Show seconds", bool(cfg.get("clock_seconds")),
                                    lambda on: cfg.set("clock_seconds", on)))
        page.add(clock)

        weather_group = Adw.PreferencesGroup(
            title="Weather",
            description="Leave the location empty to detect it from your internet connection. "
                        "Examples: “Berlin”, “Portland, OR”, “47.61, -122.33”.")
        location = Adw.EntryRow(title="Location", text=cfg.get("location") or "", show_apply_button=True)

        def apply_location(row):
            cfg.set("location", row.get_text().strip())
            self._have_weather = False
            self.refresh_weather()

        location.connect("apply", apply_location)
        units = Adw.ComboRow(title="Units", model=Gtk.StringList.new(["Celsius, km/h", "Fahrenheit, mph"]))
        units.set_selected(1 if cfg.get("units") == "fahrenheit" else 0)

        def apply_units(row, _pspec):
            cfg.set("units", "fahrenheit" if row.get_selected() == 1 else "celsius")
            self.refresh_weather()

        units.connect("notify::selected", apply_units)
        weather_group.add(location)
        weather_group.add(units)
        page.add(weather_group)
        self._prefs = dialog  # keep the wrapper alive while shown (see compat.watch_width)
        compat.present(dialog, self)


class LinuxCommApp(Adw.Application):
    def __init__(self, fullscreen: bool = False):
        flags = getattr(Gio.ApplicationFlags, "DEFAULT_FLAGS", Gio.ApplicationFlags.FLAGS_NONE)
        super().__init__(application_id=APP_ID, flags=flags)
        self.config = Config()
        self.devices = audio.DeviceRegistry()
        self.station = Station(self)
        self.server = intercom.IntercomServer(self.station)
        self.monitor = intercom.PeerMonitor(
            lambda: [(p["id"], p["address"]) for p in self.config.peers()],
            lambda pid, status: GLib.idle_add(self.to_window, "on_peer_status", pid, status),
            own_instance=self.station.instance_id)
        self.muter = muting.OtherAppsMuter(lambda: bool(self.config.get("mute_other_apps")),
                                           state_file=state_dir() / "muted-streams.json")
        self.alarms = alarms.AlarmStore()
        self.alarm_clock = alarms.AlarmClock(self.alarms, dt.datetime.now())
        self.speech = speech.SpeechToText(
            lambda call_id, caller, text, final: GLib.idle_add(self.to_window, "on_caption",
                                                               call_id, caller, text, final))
        self.tts = tts.TextToSpeech(resample=audio.resample)
        self.window: MainWindow | None = None
        self._theme_css = Gtk.CssProvider()
        self._background_state: tuple | None = None  # (image path, found) last logged
        self._start_fullscreen = fullscreen
        self._server_error: OSError | None = None

    def to_window(self, method: str, *args) -> bool:
        """Call a MainWindow method from GLib.idle_add, if the window exists."""
        if self.window is not None:
            getattr(self.window, method)(*args)
        return GLib.SOURCE_REMOVE

    # -- speech to text ------------------------------------------------------------

    def speech_model_path(self):
        """The chosen speech model, or the first one installed (None if there is none)."""
        models = speech.find_models()
        chosen = self.config.get("stt_model")
        return next((p for p in models if str(p) == chosen), models[0] if models else None)

    def apply_theme(self) -> None:
        """Recolor the whole app with the chosen theme (see themes.py)."""
        theme = themes.get(self.config.get("theme"))
        # Dark themes also switch libadwaita to its dark style, for the parts it colors itself.
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK if themes.is_dark(theme) else Adw.ColorScheme.FORCE_LIGHT)
        image = backgrounds.resolve(self.config.get("background_image") or "")
        found = image is not None and image.is_file()
        if (image, found) != self._background_state:  # log changes, not every theme or strength change
            if found:
                log.info("Loaded background image %s", image)
            elif image is not None:
                log.warning("Background image %s not found", image)
            elif self._background_state is not None:
                log.info("Background image removed")
            self._background_state = (image, found)
        compat.load_css(self._theme_css,
                        themes.css(theme, css_variables=compat.ADW_VERSION >= (1, 6))
                        + themes.text_css(self.config.get("text_color"))
                        + backgrounds.css(image if found else None, theme.background,
                                          self.config.get("background_strength")))

    def update_speech(self) -> None:
        """Load or unload the speech model to match the settings (captions, or talking with a
        synthetic voice, which needs speech to text for what you say)."""
        wanted = self.speech.available and bool(self.config.get("stt_enabled") or self.config.get("tts_relay"))
        path = self.speech_model_path() if wanted else None
        if path is None:
            self.speech.unload()
        else:
            self.speech.load(path, on_done=lambda: GLib.idle_add(self.to_window, "refresh_captions"))
        if self.window:
            self.window.refresh_captions()

    def prepare_voice(self) -> None:
        """In the background: start the chosen synthetic voice, so the first talk isn't delayed."""
        def work():
            voice = self.tts.pick(self.config.get("tts_voice"))
            if voice is not None:
                self.tts.prepare(voice)
        threading.Thread(target=work, name="tts-prepare", daemon=True).start()

    def _find_voices(self) -> None:
        """In the background: see which text-to-speech engines work, then start the voice if needed."""
        self.tts.refresh()
        if self.config.get("tts_relay"):
            voice = self.tts.pick(self.config.get("tts_voice"))
            if voice is not None:
                self.tts.prepare(voice)

    def speech_active(self) -> bool:
        return (bool(self.config.get("stt_enabled")) and self.speech.model_path is not None
                and not self.speech.error)

    def start_transcriber(self, call: intercom.IncomingCall):
        """Called on a server thread for each incoming call."""
        if not self.speech_active():
            return None
        return self.speech.start_call(call.id, call.caller, english=speech.is_english(self.speech.model_path))

    def do_startup(self):
        Adw.Application.do_startup(self)
        GLib.set_application_name(APP_NAME)
        provider = Gtk.CssProvider()
        compat.load_css(provider, CSS + calendar_view.CSS + captions_view.CSS + alarms_view.CSS + texts_view.CSS
                        + compat.CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        # The color theme goes on top of the app's own styles (the caption font is at +1).
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self._theme_css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 2)
        self.apply_theme()

        actions = (
            ("preferences", lambda: self.window and self.window.open_preferences(), ["<Ctrl>comma"]),
            ("fullscreen", self._toggle_fullscreen, ["F11"]),
            ("talk-all", lambda: self.window and self.window.toggle_talk_all(), ["<Ctrl>t"]),
            ("about", self._show_about, []),
            ("quit", self.quit, ["<Ctrl>q"]),
        )
        for name, callback, accels in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, cb=callback: cb())
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

        try:
            self.server.start()
        except OSError as e:
            log.error("Intercom server could not start on port %d: %s", self.server.port, e)
            self._server_error = e
        self.monitor.start()
        self.muter.start()
        self.update_speech()  # load the speech model in the background
        threading.Thread(target=self._find_voices, name="tts-voices", daemon=True).start()

    def do_activate(self):
        if self.window is None:
            try:
                self.window = MainWindow(self)
            except Exception:
                # Quit rather than keep running invisibly: a windowless instance would
                # hold port 80 and swallow every later launch.
                log.exception("Could not open the main window")
                self.quit()
                return
            if self._server_error:
                self.window.show_server_error(self._server_error)
            if self._start_fullscreen:
                self.window.fullscreen()
        self.window.present()

    def do_shutdown(self):
        if self.window:
            self.window.stop_talk(sync=False)
            self.window._stop_mic_test()
            if self.window._alarm_sound is not None:
                self.window._alarm_sound.stop()
            if self.window._dictation is not None:
                self.window._dictation.stop()
        self.monitor.stop()
        self.server.stop()
        self.muter.stop()  # unmutes anything still muted
        self.tts.stop()
        Adw.Application.do_shutdown(self)

    def _toggle_fullscreen(self) -> None:
        if self.window:
            if self.window.is_fullscreen():
                self.window.unfullscreen()
            else:
                self.window.fullscreen()

    def _show_about(self) -> None:
        self._about = compat.about_dialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            version=__version__,
            release_notes_version=__version__,
            release_notes=changelog.release_notes(__version__),
            comments="Clock, weather and intercom for your local network.\n\nWeather data by Open-Meteo.com",
        )
        compat.present(self._about, self.window)


def _setup_logging(debug: bool) -> None:
    """Log to the terminal and to ~/.local/state/linuxcomm/linuxcomm.log.

    The file matters when LinuxComm is started from the menu or at login, where
    nobody sees the terminal output.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            state_dir() / "linuxcomm.log", maxBytes=512_000, backupCount=1, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    # Errors in GTK callbacks go through sys.excepthook; send them to the log as well.
    sys.excepthook = lambda *exc_info: log.error("Unhandled error", exc_info=exc_info)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    parser = argparse.ArgumentParser(prog="linuxcomm", description="Clock, weather and LAN intercom.")
    parser.add_argument("--fullscreen", action="store_true", help="start fullscreen (kiosk displays)")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv[1:])
    _setup_logging(args.debug)

    if compat.ADW_VERSION < compat.MIN_ADW_VERSION or compat.GTK_VERSION < compat.MIN_GTK_VERSION:
        log.error("LinuxComm needs libadwaita %d.%d and GTK %d.%d or newer; this system has libadwaita %d.%d "
                  "and GTK %d.%d.", *compat.MIN_ADW_VERSION, *compat.MIN_GTK_VERSION,
                  *compat.ADW_VERSION, *compat.GTK_VERSION)
        return 1

    app = LinuxCommApp(fullscreen=args.fullscreen)
    if hasattr(GLib, "unix_signal_add"):
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: (app.quit(), GLib.SOURCE_REMOVE)[1])
    return app.run(argv[:1])
