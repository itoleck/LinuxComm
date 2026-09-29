"""Text messages: the full-width pop-up for an incoming text, the composer, and an on-screen keyboard."""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

CSS = """
.text-modal { background-color: rgba(0, 0, 0, 0.5); }
.text-card { background-color: @card_bg_color; color: @card_fg_color; padding: 26px 36px;
             box-shadow: 0 2px 18px rgba(0, 0, 0, 0.4); }
.text-from { font-size: 15pt; font-weight: bold; }
.text-body { font-size: 24pt; }
.text-when { font-size: 11pt; }
.text-title { font-size: 15pt; font-weight: bold; }
.text-editor-frame { border-radius: 10px; background-color: @view_bg_color;
                     box-shadow: inset 0 0 0 1px alpha(@window_fg_color, 0.2); }
.text-editor { font-size: 16pt; background: transparent; }
.text-partial { font-style: italic; }
.osk-key { min-height: 44px; min-width: 30px; padding: 0 2px; font-size: 14pt; font-weight: normal; }
.narrow .text-card { padding: 18px 12px; }
.narrow .text-body { font-size: 18pt; }
.narrow .osk-key { min-width: 20px; padding: 0; }
.narrow .text-card button.pill { padding: 8px 14px; }
"""

LETTERS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
SYMBOLS = ("1234567890", "@#$%&-+()/", "*\"':;!?")


class OnScreenKeyboard(Gtk.Box):
    """A small touch keyboard for stations without a physical one."""

    def __init__(self, on_text: Callable[[str], None], on_backspace: Callable[[], None]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=6)
        self._on_text = on_text
        self._on_backspace = on_backspace
        self._shift = False
        self._symbols = False
        self._build()

    def _key(self, label: str, action: Callable[[], None], wide: bool = True, css: tuple = ()) -> Gtk.Button:
        # focus_on_click=False keeps the cursor in the message while typing here.
        button = Gtk.Button(label=label, focus_on_click=False, hexpand=wide, css_classes=["osk-key", *css])
        button.connect("clicked", lambda *_: action())
        return button

    def _build(self) -> None:
        while child := self.get_first_child():
            self.remove(child)
        for i, keys in enumerate(SYMBOLS if self._symbols else LETTERS):
            row = Gtk.Box(spacing=4)
            if i == 2 and not self._symbols:
                row.append(self._key("⇧", self._toggle_shift, css=("suggested-action",) if self._shift else ()))
            for key in keys:
                char = key.upper() if self._shift else key
                row.append(self._key(char, lambda c=char: self._type(c)))
            if i == 2:
                row.append(self._key("⌫", self._on_backspace))
            self.append(row)
        bottom = Gtk.Box(spacing=4)
        bottom.append(self._key("abc" if self._symbols else "?123", self._toggle_symbols, wide=False))
        bottom.append(self._key(",", lambda: self._type(","), wide=False))
        bottom.append(self._key("space", lambda: self._type(" ")))
        bottom.append(self._key(".", lambda: self._type("."), wide=False))
        bottom.append(self._key("↵", lambda: self._type("\n"), wide=False))
        self.append(bottom)

    def _type(self, text: str) -> None:
        self._on_text(text)
        if self._shift:  # one capital letter, like a phone keyboard
            self._shift = False
            self._build()

    def _toggle_shift(self) -> None:
        self._shift = not self._shift
        self._build()

    def _toggle_symbols(self) -> None:
        self._symbols, self._shift = not self._symbols, False
        self._build()


class TextModal(Gtk.Box):
    """Covers the whole window: an incoming text (click anywhere to close it) or the composer."""

    def __init__(self, on_close_message: Callable[[], None], on_reply: Callable[[], None],
                 on_send: Callable[[str], None], on_cancel: Callable[[], None],
                 on_dictate: Callable[[bool], None], max_length: int):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, visible=False, css_classes=["text-modal"])
        self._on_close_message = on_close_message
        self._on_send = on_send
        self._max_length = max_length
        self._busy = False
        self._syncing = False
        # One page at a time, in a plain box: a Gtk.Stack would size a hidden wrapping message
        # as a single line (as wide as the whole text) on GTK 4.14 and older.
        self._page = "message"
        self._pages: dict[str, Gtk.Widget] = {}
        pages = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.append(Gtk.ScrolledWindow(child=pages, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))

        # -- an incoming text --
        message = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["text-card"])
        self._from = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, css_classes=["text-from"])
        self._body = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=False,
                               css_classes=["text-body"])
        bottom = Gtk.Box(spacing=16, margin_top=6)
        self._more = Gtk.Label(xalign=0, hexpand=True, wrap=True, css_classes=["dim-label", "caption"])
        self.reply_button = Gtk.Button(css_classes=["pill", "suggested-action"], valign=Gtk.Align.CENTER)
        self.reply_button.set_child(_button_content("mail-reply-sender-symbolic", "Reply"))
        self.reply_button.connect("clicked", lambda *_: on_reply())
        self._when = Gtk.Label(css_classes=["dim-label", "text-when"], valign=Gtk.Align.CENTER, wrap=True,
                               justify=Gtk.Justification.RIGHT, xalign=1)
        for widget in (self._more, self.reply_button, self._when):
            bottom.append(widget)
        for widget in (self._from, self._body, bottom):
            message.append(widget)
        self._pages["message"] = message

        # -- the composer --
        compose = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["text-card"])
        self._title = Gtk.Label(xalign=0, css_classes=["text-title"], ellipsize=Pango.EllipsizeMode.END)
        compose.append(self._title)
        self.editor = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False, top_margin=8,
                                   bottom_margin=8, left_margin=10, right_margin=10, css_classes=["text-editor"])
        self.editor.set_input_hints(Gtk.InputHints.SPELLCHECK | Gtk.InputHints.UPPERCASE_SENTENCES)
        self.editor.get_buffer().connect("changed", lambda *_: self._update_send())
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_editor_key)
        self.editor.add_controller(keys)
        frame = Gtk.ScrolledWindow(child=self.editor, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                   css_classes=["text-editor-frame"])
        frame.set_size_request(-1, 110)
        compose.append(frame)
        self._partial = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label", "text-partial"], visible=False)
        compose.append(self._partial)
        actions = Gtk.Box(spacing=8)
        self._dictate = Gtk.ToggleButton(css_classes=["pill"], valign=Gtk.Align.CENTER)
        self._dictate.set_child(_button_content("audio-input-microphone-symbolic", "Dictate"))
        self._dictate.connect("toggled", lambda b: None if self._syncing else on_dictate(b.get_active()))
        self._keyboard = Gtk.ToggleButton(css_classes=["pill"], valign=Gtk.Align.CENTER,
                                          tooltip_text="On-screen keyboard")
        self._keyboard.set_child(_button_content("input-keyboard-symbolic", "Keyboard"))
        self._count = Gtk.Label(css_classes=["dim-label", "caption"], hexpand=True, xalign=1)
        cancel = Gtk.Button(label="Cancel", css_classes=["pill"], valign=Gtk.Align.CENTER)
        cancel.connect("clicked", lambda *_: on_cancel())
        self.send_button = Gtk.Button(css_classes=["pill", "suggested-action"], valign=Gtk.Align.CENTER,
                                      tooltip_text="Send (Enter). Shift+Enter starts a new line.")
        self.send_button.set_child(_button_content("mail-send-symbolic", "Send"))
        self.send_button.connect("clicked", lambda *_: self._send_now())
        for widget in (self._dictate, self._keyboard, self._count, cancel, self.send_button):
            actions.append(widget)
        compose.append(actions)
        self.keyboard = OnScreenKeyboard(on_text=self.insert_text, on_backspace=self._backspace)
        self._osk = Gtk.Revealer(child=self.keyboard, transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._keyboard.connect("toggled", lambda b: self._osk.set_reveal_child(b.get_active()))
        compose.append(self._osk)
        self._pages["compose"] = compose
        for page in self._pages.values():
            pages.append(page)

        # A click or tap anywhere on an incoming text (except Reply) closes it.
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: self.showing("message") and self._on_close_message())
        self.add_controller(click)

    def showing(self, page: str | None = None) -> bool:
        return self.get_visible() and (page is None or self._page == page)

    def _show_page(self, name: str) -> None:
        self._page = name
        for key, page in self._pages.items():
            page.set_visible(key == name)
        self.set_visible(True)

    def show_message(self, sender: str, text: str, when: str, more: int) -> None:
        self._from.set_label(f"Text from: {sender}")
        self._body.set_label(text)
        self._when.set_label(when)
        self.set_more(more)
        self._show_page("message")
        self._focus(self.reply_button)

    def set_more(self, more: int) -> None:
        self._more.set_label(f"{more} more message{'s' if more != 1 else ''}" if more else "")

    def set_narrow(self, narrow: bool) -> None:
        """On a narrow screen Dictate and Keyboard show just their icons, so the buttons fit one row."""
        for button in (self._dictate, self._keyboard):
            button.get_child().get_last_child().set_visible(not narrow)

    def show_compose(self, title: str, dictation_problem: str | None) -> None:
        self._title.set_label(title)
        self.editor.get_buffer().set_text("")
        self.set_partial("")
        self.set_busy(False)
        self.set_dictating(False)
        self._dictate.set_sensitive(dictation_problem is None)
        self._dictate.set_tooltip_text(dictation_problem or "Speak your message")
        self._show_page("compose")
        self._focus(self.editor)

    def _focus(self, widget: Gtk.Widget) -> None:
        widget.grab_focus()
        # GTK 4.18 moves the focus away from a hidden page (e.g. the Reply just clicked) a moment
        # later, so take it again once that has happened.
        GLib.idle_add(lambda: self.get_visible() and widget.get_mapped() and widget.grab_focus() and False)

    def text(self) -> str:
        buffer = self.editor.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def insert_text(self, text: str) -> None:
        buffer = self.editor.get_buffer()
        buffer.insert_at_cursor(text)
        self.editor.scroll_mark_onscreen(buffer.get_insert())

    def insert_words(self, text: str) -> None:
        """Dictated words, separated by a space from what is already there."""
        buffer = self.editor.get_buffer()
        before = buffer.get_text(buffer.get_start_iter(), buffer.get_iter_at_mark(buffer.get_insert()), False)
        self.insert_text((" " if before and not before[-1].isspace() else "") + text)

    def _backspace(self) -> None:
        buffer = self.editor.get_buffer()
        if not buffer.delete_selection(True, True):
            buffer.backspace(buffer.get_iter_at_mark(buffer.get_insert()), True, True)

    def set_partial(self, text: str) -> None:
        """Words heard while dictating that are not final yet."""
        self._partial.set_label(text)
        self._partial.set_visible(bool(text))

    def set_dictating(self, on: bool) -> None:
        self._syncing = True
        try:
            self._dictate.set_active(on)
        finally:
            self._syncing = False
        for css in ("destructive-action", "hangup"):
            (self._dictate.add_css_class if on else self._dictate.remove_css_class)(css)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_send()

    def _update_send(self) -> None:
        text = self.text()
        self._count.set_label(f"{len(text)}/{self._max_length}" if len(text) > self._max_length * 0.8 else "")
        (self._count.add_css_class if len(text) > self._max_length else self._count.remove_css_class)("error")
        self.send_button.set_sensitive(not self._busy and bool(text.strip()) and len(text) <= self._max_length)

    def _send_now(self) -> None:
        if self.send_button.get_sensitive():
            self._on_send(self.text())

    def _on_editor_key(self, _controller, keyval, _keycode, state) -> bool:
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not state & Gdk.ModifierType.SHIFT_MASK:
            self._send_now()  # Enter sends; Shift+Enter starts a new line
            return True
        return False


def _button_content(icon: str, label: str) -> Gtk.Widget:
    box = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
    box.append(Gtk.Image(icon_name=icon))
    box.append(Gtk.Label(label=label))
    return box


def when_text(moment: GLib.DateTime, clock_24h: bool) -> str:
    """"14:05 · Monday, September 28, 2026" (or 2:05 PM)."""
    time = moment.format("%H:%M" if clock_24h else "%l:%M %p").strip()
    return f"{time} · {moment.format('%A, %B %-d, %Y')}"
