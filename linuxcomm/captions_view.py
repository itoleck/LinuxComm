"""The captions bar at the bottom of the window, and the transcript panel."""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from .speech import Line  # noqa: E402

CSS = """
.captions { border-radius: 0; padding: 8px 18px; font-weight: normal;
            border-top: 1px solid alpha(@window_fg_color, 0.12); background-color: @view_bg_color; }
.caption-line.partial { font-style: italic; }
.caption-line.placeholder { opacity: 0.55; }
"""


def style_css(family: str, size: int) -> str:
    """CSS for the caption text in the chosen font and size ("" = the desktop's font)."""
    family = family.replace("\\", "").replace('"', "")
    font = f'font-family: "{family}"; ' if family else ""
    return f".caption-line {{ {font}font-size: {max(6, int(size))}pt; }}"


class CaptionsPanel(Gtk.Button):
    """Two lines of captions, newest at the bottom; click it for the whole transcript."""

    def __init__(self):
        super().__init__(css_classes=["flat", "captions"], tooltip_text="Show the whole transcript")
        box = Gtk.Box(spacing=14)
        self._icon = Gtk.Image(valign=Gtk.Align.CENTER, css_classes=["dim-label"])
        lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self._rows = [Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.START, css_classes=["caption-line"])
                      for _ in range(2)]
        for row in self._rows:
            lines.append(row)
        box.append(self._icon)
        box.append(lines)
        box.append(Gtk.Image(icon_name="go-next-symbolic", valign=Gtk.Align.CENTER, css_classes=["dim-label"]))
        self.set_child(box)
        self.connect("realize", lambda *_: self._pick_icon())

    def _pick_icon(self) -> None:
        theme = Gtk.IconTheme.get_for_display(self.get_display())
        names = ("media-view-subtitles-symbolic", "format-justify-left-symbolic")
        self._icon.set_from_icon_name(next((n for n in names if theme.has_icon(n)), names[-1]))

    def _set_row(self, row: Gtk.Label, markup: str, *classes: str) -> None:
        row.set_markup(markup)
        for css in ("partial", "placeholder"):
            (row.add_css_class if css in classes else row.remove_css_class)(css)

    def show_placeholder(self, text: str) -> None:
        self._set_row(self._rows[0], "")
        self._set_row(self._rows[1], GLib.markup_escape_text(text), "placeholder")

    def show_lines(self, items: list[tuple[Line, bool]]) -> None:
        """Show up to two (line, still_being_spoken) pairs, oldest first."""
        items = [None] * (len(self._rows) - len(items)) + list(items)[-len(self._rows):]
        for row, item in zip(self._rows, items):
            if item is None:
                self._set_row(row, "")
                continue
            line, partial = item
            markup = f"<b>{GLib.markup_escape_text(line.caller)}:</b> {GLib.markup_escape_text(line.text)}"
            self._set_row(row, markup, *(["partial"] if partial else []))


class TranscriptPanel(Gtk.Box):
    """The whole conversation, shown in the calendar's place over the clock and weather.

    Finished sentences are followed by the ones still being spoken (in italics), which
    update live. It follows new text unless the reader has scrolled up.
    """

    def __init__(self, on_close: Callable[[], None], on_save: Callable[[], None],
                 on_copy: Callable[[], None], on_clear: Callable[[], None]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                         css_classes=["card", "calendar-panel", "transcript-panel"])
        header = Gtk.Box(spacing=6)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        titles.append(Gtk.Label(label="Transcript", xalign=0, css_classes=["calendar-title"],
                                ellipsize=Pango.EllipsizeMode.END))
        self._subtitle = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"], ellipsize=Pango.EllipsizeMode.END)
        titles.append(self._subtitle)
        header.append(titles)
        save = Gtk.Button(label="Save", tooltip_text="Save to a text file", css_classes=["flat"],
                          valign=Gtk.Align.CENTER)
        save.connect("clicked", lambda *_: on_save())
        header.append(save)
        for icon, tooltip, callback in (("edit-copy-symbolic", "Copy the transcript", on_copy),
                                        ("edit-clear-all-symbolic", "Clear the transcript", on_clear)):
            button = Gtk.Button(icon_name=icon, tooltip_text=tooltip, css_classes=["flat", "circular"],
                                valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda *_, cb=callback: cb())
            header.append(button)
        self.close_button = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Close (Esc)",
                                       css_classes=["flat", "circular"], valign=Gtk.Align.CENTER)
        self.close_button.connect("clicked", lambda *_: on_close())
        header.append(self.close_button)
        self.append(header)

        self.view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 css_classes=["caption-line", "transcript-text"])
        buffer = self.view.get_buffer()
        self._live_tag = buffer.create_tag("live", style=Pango.Style.ITALIC)
        self._end = buffer.create_mark("end", buffer.get_end_iter(), False)  # stays at the end
        self._finished_chars = 0  # the live sentences start here
        self._scroller = Gtk.ScrolledWindow(child=self.view, vexpand=True,
                                            hscrollbar_policy=Gtk.PolicyType.NEVER)
        empty = Gtk.Label(label="Nothing has been said to this station in this conversation yet.",
                          wrap=True, justify=Gtk.Justification.CENTER, valign=Gtk.Align.CENTER,
                          css_classes=["dim-label"], margin_start=12, margin_end=12)
        self._stack = Gtk.Stack(vexpand=True)
        self._stack.add_named(empty, "empty")
        self._stack.add_named(self._scroller, "text")
        self.append(self._stack)

    def text(self) -> str:
        buffer = self.view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def show(self, finished: list[str], live: list[str], subtitle: str) -> None:
        """Replace everything."""
        follow = self._at_end()
        buffer = self.view.get_buffer()
        buffer.set_text("\n".join(finished))
        self._finished_chars = buffer.get_char_count()
        self._write_live(live)
        self._subtitle.set_label(subtitle)
        self._after_change(follow)

    def show_live(self, live: list[str]) -> None:
        """Update only the sentences still being spoken."""
        follow = self._at_end()
        self._write_live(live)
        self._after_change(follow)

    def _write_live(self, live: list[str]) -> None:
        buffer = self.view.get_buffer()
        buffer.delete(buffer.get_iter_at_offset(self._finished_chars), buffer.get_end_iter())
        for line in live:
            if buffer.get_char_count():
                buffer.insert(buffer.get_end_iter(), "\n")
            buffer.insert_with_tags(buffer.get_end_iter(), line, self._live_tag)

    def _at_end(self) -> bool:
        adjustment = self._scroller.get_vadjustment()
        return adjustment.get_value() >= adjustment.get_upper() - adjustment.get_page_size() - 8

    def _after_change(self, follow: bool) -> None:
        self._stack.set_visible_child_name("text" if self.view.get_buffer().get_char_count() else "empty")
        if follow:
            # Scroll once the text has been laid out; doing it right away scrolls too far.
            GLib.idle_add(lambda: self.view.scroll_to_mark(self._end, 0.0, True, 0.0, 1.0) and False,
                          priority=GLib.PRIORITY_LOW)
