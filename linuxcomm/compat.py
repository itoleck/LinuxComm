"""Fallbacks for older libadwaita and GTK.

The interface uses current libadwaita widgets where they exist, and falls back to
equivalents on libadwaita 1.2 / GTK 4.8 as shipped by Debian 12 and Raspberry Pi OS
Bookworm. Newer libadwaita APIs used here: Banner (1.3); ToolbarView, Breakpoint,
SwitchRow, SpinRow and Toast:use-markup (1.4); AlertDialog, PreferencesDialog and
AboutDialog (1.5).
"""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, GObject, Gtk, Pango  # noqa: E402

ADW_VERSION = (Adw.get_major_version(), Adw.get_minor_version())
GTK_VERSION = (Gtk.get_major_version(), Gtk.get_minor_version())
MIN_ADW_VERSION = (1, 2)
MIN_GTK_VERSION = (4, 8)

# Styles for the fallback widgets; harmless when the real ones are used.
CSS = """
.lc-banner { background-color: alpha(@accent_bg_color, 0.25); padding: 6px 12px; }
"""


def load_css(provider: Gtk.CssProvider, css: str) -> None:
    if hasattr(provider, "load_from_string"):  # GTK 4.12+
        provider.load_from_string(css)
        return
    try:
        provider.load_from_data(css, -1)
    except TypeError:  # older annotation: a byte array without a length argument
        provider.load_from_data(css.encode())


def toast(message: str, timeout: int) -> Adw.Toast:
    if ADW_VERSION >= (1, 4):
        return Adw.Toast(title=message, use_markup=False, timeout=timeout)
    return Adw.Toast(title=GLib.markup_escape_text(message), timeout=timeout)


# -- banner ---------------------------------------------------------------------

class _Banner(Gtk.Revealer):
    """Stand-in for Adw.Banner with the subset of its API that LinuxComm uses."""

    __gsignals__ = {"button-clicked": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        box = Gtk.Box(spacing=12, css_classes=["lc-banner"])
        self._label = Gtk.Label(hexpand=True, wrap=True, justify=Gtk.Justification.CENTER, css_classes=["heading"])
        self._button = Gtk.Button(valign=Gtk.Align.CENTER, visible=False)
        self._button.connect("clicked", lambda *_: self.emit("button-clicked"))
        box.append(self._label)
        box.append(self._button)
        self.set_child(box)

    def set_title(self, title: str) -> None:
        self._label.set_label(title)

    def get_title(self) -> str:
        return self._label.get_label()

    def set_button_label(self, label: str | None) -> None:
        self._button.set_label(label or "")
        self._button.set_visible(bool(label))

    def get_button_label(self) -> str | None:
        return self._button.get_label() if self._button.get_visible() else None

    def set_revealed(self, revealed: bool) -> None:
        self.set_reveal_child(revealed)

    def get_revealed(self) -> bool:
        return self.get_reveal_child()


def banner(button_label: str | None = None) -> Gtk.Widget:
    b = Adw.Banner(use_markup=False) if hasattr(Adw, "Banner") else _Banner()
    b.set_button_label(button_label)
    return b


# -- window layout ----------------------------------------------------------------

def toolbar_view(top_bars: list[Gtk.Widget], content: Gtk.Widget,
                 bottom_bars: list[Gtk.Widget] | tuple = ()) -> Gtk.Widget:
    if hasattr(Adw, "ToolbarView"):
        view = Adw.ToolbarView()
        for bar in top_bars:
            view.add_top_bar(bar)
        view.set_content(content)
        for bar in bottom_bars:
            view.add_bottom_bar(bar)
        return view
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    for bar in top_bars:
        box.append(bar)
    content.set_vexpand(True)
    box.append(content)
    for bar in bottom_bars:
        box.append(bar)
    return box


class _WidthWatcher(Gtk.Widget):
    """Stand-in for Adw.Breakpoint: reports when its width crosses a threshold.

    Like AdwBreakpointBin it does not demand its child's full minimum width, so the
    window can be made narrow enough for the narrow layout to take over.
    """

    def __init__(self, child: Gtk.Widget, threshold: int, on_change: Callable[[bool], None]):
        super().__init__(overflow=Gtk.Overflow.HIDDEN)
        self._child = child
        self._threshold = threshold
        self._on_change = on_change
        self._narrow: bool | None = None
        child.set_parent(self)
        self.connect("destroy", lambda *_: self._child.unparent())

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            _minimum, natural, _mb, _nb = self._child.measure(orientation, for_size)
            return 0, natural, -1, -1
        if for_size >= 0:
            for_size = max(for_size, self._child.measure(Gtk.Orientation.HORIZONTAL, -1)[0])
        return self._child.measure(orientation, for_size)

    def do_size_allocate(self, width, height, baseline):
        narrow = width <= self._threshold
        if narrow != self._narrow:
            self._narrow = narrow
            GLib.idle_add(self._on_change, narrow)  # never relayout from inside an allocation
        minimum = self._child.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        self._child.allocate(max(width, minimum), height, baseline, None)


def watch_width(window: Adw.ApplicationWindow, content: Gtk.Widget, threshold: int,
                on_change: Callable[[bool], None]) -> Gtk.Widget:
    """Call on_change(True) when content is at most `threshold` wide, on_change(False) when wider.

    Returns the widget to put in the window in place of `content`.
    """
    if hasattr(Adw, "Breakpoint"):
        narrow_bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(f"max-width: {threshold}sp"))
        narrow_bp.connect("apply", lambda *_: on_change(True))
        narrow_bp.connect("unapply", lambda *_: on_change(False))
        window.add_breakpoint(narrow_bp)
        # Keep the Python wrapper alive: letting it be collected crashed libadwaita 1.5.
        window._lc_breakpoint = narrow_bp
        return content
    return _WidthWatcher(content, threshold, on_change)


# -- preference rows ----------------------------------------------------------------

def switch_row(title: str, active: bool, on_toggled: Callable[[bool], None], subtitle: str = "") -> Adw.ActionRow:
    if hasattr(Adw, "SwitchRow"):
        row = source = Adw.SwitchRow(title=title, active=active)
    else:
        row = Adw.ActionRow(title=title)
        source = Gtk.Switch(active=active, valign=Gtk.Align.CENTER)
        row.add_suffix(source)
        row.set_activatable_widget(source)
    if subtitle:
        row.set_subtitle(subtitle)
    source.connect("notify::active", lambda widget, _p: on_toggled(widget.get_active()))
    return row


def spin_row(title: str, subtitle: str, lower: float, upper: float, step: float, value: float,
             on_changed: Callable[[float], None]) -> Adw.ActionRow:
    if hasattr(Adw, "SpinRow"):
        row = source = Adw.SpinRow.new_with_range(lower, upper, step)
        row.set_title(title)
    else:
        row = Adw.ActionRow(title=title)
        source = Gtk.SpinButton.new_with_range(lower, upper, step)
        source.set_valign(Gtk.Align.CENTER)
        row.add_suffix(source)
    row.set_subtitle(subtitle)
    source.set_value(value)
    source.connect("notify::value", lambda widget, _p: on_changed(widget.get_value()))
    return row


# -- dialogs ------------------------------------------------------------------------

def alert_dialog(heading: str, body: str):
    """Adw.AlertDialog, or Adw.MessageDialog (same response API) on libadwaita < 1.5."""
    cls = getattr(Adw, "AlertDialog", None) or Adw.MessageDialog
    return cls(heading=heading, body=body)


def preferences_dialog(title: str):
    if hasattr(Adw, "PreferencesDialog"):
        return Adw.PreferencesDialog(title=title)
    return Adw.PreferencesWindow(title=title)


def font_family_button(family: str, on_changed: Callable[[str], None]) -> Gtk.Widget:
    """A button that picks a font family ("" = the desktop's font); calls on_changed(family)."""
    shown = family or Pango.FontDescription.from_string(
        Gtk.Settings.get_default().get_property("gtk-font-name") or "Sans").get_family() or "Sans"
    # Gtk.FontDialogButton exists since GTK 4.10, but before 4.16 it logs a GLib critical when
    # it shows a font at the family level, so the older (deprecated) Gtk.FontButton is used there.
    if hasattr(Gtk, "FontDialogButton") and GTK_VERSION >= (4, 16):
        button = Gtk.FontDialogButton(dialog=Gtk.FontDialog(title="Caption Font"), level=Gtk.FontLevel.FAMILY,
                                      valign=Gtk.Align.CENTER)
        button.set_font_desc(Pango.FontDescription.from_string(shown))
        button.connect("notify::font-desc",
                       lambda b, _p: on_changed(b.get_font_desc().get_family() if b.get_font_desc() else ""))
    else:
        button = Gtk.FontButton(level=Gtk.FontChooserLevel.FAMILY, title="Caption Font", valign=Gtk.Align.CENTER)
        button.set_font(shown)
        button.connect("font-set",
                       lambda b: on_changed(Pango.FontDescription.from_string(b.get_font() or "").get_family() or ""))
    return button


def about_dialog(**properties):
    cls = getattr(Adw, "AboutDialog", None) or Adw.AboutWindow
    return cls(**properties)


def present(dialog, parent: Gtk.Window) -> None:
    """Show a dialog from one of the helpers above over `parent`."""
    if hasattr(Adw, "Dialog") and isinstance(dialog, Adw.Dialog):
        dialog.present(parent)
    else:
        dialog.set_transient_for(parent)
        dialog.set_modal(True)
        dialog.present()
