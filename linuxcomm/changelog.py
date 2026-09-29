"""Version history, shown in About → What's New.

Newest release first. The first entry's version must equal linuxcomm.__version__
(tests/test_version.py checks this).
"""

from __future__ import annotations

from html import escape

# (version, date, changes)
RELEASES: list[tuple[str, str, list[str]]] = [
    ("0.0.24", "2026-09-28", [
        "While you record a message for the synthetic voice, the station's Talk button reads Send (and Talk "
        "to all stations reads Send to all stations): press it to send the message.",
    ]),
    ("0.0.23", "2026-09-28", [
        "Talking with a synthetic voice now records first: press Talk, speak (the status line shows what "
        "is recognized), and press Talk again. Only then is everything read out by the voice and sent as "
        "one message, instead of each sentence being sent while you were still talking.",
        "The other station isn't called while you record, and \"Sent to Kitchen: …\" confirms what was sent.",
        "The voice list shows the voices' full names, with the chosen voice under Voice, and searching finds "
        "a voice by any part of its name (e.g. \"english\").",
    ]),
    ("0.0.22", "2026-09-28", [
        "Talk with a synthetic voice (Preferences → Talking → Text-to-speech voice): what you say is "
        "turned into text on this computer and read out by a voice, and only that voice is sent, never "
        "your microphone's sound, so there is no echo or feedback. Each sentence goes out when you finish it.",
        "Voices: 109 natural English voices from Coqui TTS (installed on 64-bit systems with 4 GB of "
        "memory), and 141 eSpeak voices through pyttsx3, used when Coqui isn't installed or doesn't "
        "start. Choose one in Preferences and press ▶ to hear it.",
        "While talking, the status line shows the last sentence the voice said.",
        "Installer: new --no-coqui option to leave out (or remove) the natural voices, which take about "
        "2 GB of disk space.",
    ]),
    ("0.0.21", "2026-09-28", [
        "Echo cancellation now works with the current WebRTC audio processing (1.x) of Raspberry Pi OS "
        "Trixie, Debian 13, Ubuntu 25.10+ and Arch, which finds the echo delay by itself: LinuxComm only "
        "measures and sets the delay for the older library of Raspberry Pi OS Bookworm. Before, "
        "LinuxComm's adjustments stopped the newer library from cancelling anything.",
        "The log says which of the two ways is in use when a talk starts.",
    ]),
    ("0.0.20", "2026-09-28", [
        "Raspberry Pi OS Trixie (Debian 13, 64-bit or 32-bit) is a supported system: every feature, "
        "including captions and echo cancellation, works there, and with its Python 3.13.",
        "After upgrading a system (e.g. Raspberry Pi OS Bookworm to Trixie), run the installer again; "
        "it rebuilds the speech-to-text environment for the new Python version.",
    ]),
    ("0.0.19", "2026-09-28", [
        "Echo cancellation while you talk: whatever this computer's speaker plays (another station "
        "talking to you, the chime, music or videos) is removed from your microphone, so it isn't "
        "sent on. It uses WebRTC audio processing and measures the speaker-to-microphone delay itself.",
        "On by default where available; Preferences → Talking → Echo cancellation turns it off (from "
        "the next talk). The installers add GStreamer's extra plugins for it; Ubuntu 24.04 doesn't "
        "include the needed plugin, so there it shows as not available.",
    ]),
    ("0.0.18", "2026-09-28", [
        "Acoustic feedback suppression for your microphone while you talk: when howling starts (for "
        "example because another station's speaker is within earshot), a narrow notch filter removes "
        "that frequency within about a third of a second, and speech is left unchanged. It is on by "
        "default; Preferences → Talking turns it on or off, even during a talk.",
        "The talk status line says \"Feedback suppressed\" when it has acted, and the log names the "
        "frequency.",
    ]),
    ("0.0.17", "2026-09-28", [
        "No more feedback loops: while another station talks to you, the Talk buttons and Talk to all "
        "are disabled until it finishes. Stopping your own talk still works.",
        "Reply now ends every current talk, incoming and your own, and then talks to the caller, who "
        "sees \"Living Room is replying\".",
        "Hang Up, to the left of Reply, ends the incoming call: its audio stops at once and the caller "
        "sees \"Living Room hung up\".",
    ]),
    ("0.0.16", "2026-09-28", [
        "The log now says which background image was loaded, with its full path (\"Loaded background "
        "image /home/pi/linuxcomm/data/images/sunset.jpg\"), and where a downloaded image came from. "
        "It is logged when the image changes, not on every theme or strength change.",
    ]),
    ("0.0.15", "2026-09-28", [
        "The text color setting now only colors text that sits directly on the window background: "
        "the Intercom and Audio devices headings, the Intercom description, and the talking status "
        "line. Buttons, cards and panels keep the theme's colors.",
    ]),
    ("0.0.14", "2026-09-28", [
        "Text color setting (Preferences → Appearance): Black (the default) or White, or any color as "
        "an HTML hex code such as #1a2b3c or from the color picker. It colors the clock, weather, "
        "stations and all other text in the main window.",
        "Text on colored buttons and highlights keeps its own readable color, and dialogs and menus "
        "keep the theme's colors, so Preferences is always readable.",
        "Choosing a dark theme switches black text to white (and a light theme switches white to "
        "black); a color that is hard to read on the theme is pointed out.",
    ]),
    ("0.0.13", "2026-09-28", [
        "Text messages: the Text button next to a station's Talk button sends it a message.",
        "An incoming text covers the whole window, full width and centered, with who sent it, the "
        "time and date, and Reply. Click or tap it (or press Esc) to close it; texts that arrive "
        "meanwhile wait their turn.",
        "Reply by keyboard, with the built-in on-screen keyboard for touchscreens, or by dictation "
        "(offline speech to text, also when captions are off).",
        "Do not disturb refuses texts as well as calls.",
    ]),
    ("0.0.12", "2026-09-28", [
        "Three daily alarms (Alarm 1–3) above the time. Click one to create, change, turn on or off, "
        "or delete it in the left panel. They are saved in ~/linuxcomm/data/alarms.json.",
        "When an alarm rings, the left panel offers Snooze 10 minutes or Dismiss; snoozing doesn't "
        "change the saved alarm. An unanswered alarm stops after 15 minutes.",
        "The bell next to a station's Talk button opens that station's alarms, to set them remotely.",
    ]),
    ("0.0.11", "2026-09-28", [
        "Background image for the whole window (Preferences → Appearance): enter a file name from "
        "~/linuxcomm/data/images or a full path, or paste an image's URL and press Download to save "
        "it there and use it.",
        "Image strength setting: how strongly the image shows through the theme's background color, "
        "so text stays readable over busy pictures.",
    ]),
    ("0.0.10", "2026-09-28", [
        "Color themes: Classic, Soft Dark, Forest, Ocean, Sunset, Monochrome and Lavender "
        "(Preferences → Appearance). They recolor the window, cards, buttons, text, borders "
        "and today in the calendars, and apply right away.",
        "The Stop talking button is always the same solid red, in every theme.",
    ]),
    ("0.0.9", "2026-09-28", [
        "Saved transcripts now go to LinuxComm's working folder in your home, ~/linuxcomm/data "
        "(e.g. /home/pi/linuxcomm/data), created on the first save, instead of /data.",
        "The installers no longer create /data, and remove the empty /data that version 0.0.8 made.",
    ]),
    ("0.0.8", "2026-09-28", [
        "The transcript now opens in the calendar's place on the left, over the clock and weather; "
        "click the captions bar again (or press Esc) to close it.",
        "During a call the transcript updates live, including the sentence still being spoken.",
        "New Save button: writes the transcript to a text file in /data, named with the date and time "
        "the conversation started and who spoke (e.g. 2026-09-28_14-05-33_Kitchen.txt). The folder "
        "can be changed in Preferences → Captions; the installers create /data.",
    ]),
    ("0.0.7", "2026-09-28", [
        "Live captions: a bar at the bottom of the window shows the last two sentences other "
        "stations say, as they speak. Speech recognition runs offline on this computer (Vosk).",
        "Click or tap the captions to see the whole conversation, with times and names; copy or clear it there.",
        "Preferences → Captions: turn speech to text on or off, and choose the font, size and language model.",
        "The installers set up speech to text (a ~40 MB download); --no-speech leaves it out.",
    ]),
    ("0.0.6", "2026-09-27", [
        "Stations are now reached under the path /linuxcomm, so a station can sit behind a reverse "
        "proxy such as nginx and be added as e.g. http://swarmsoft.com/linuxcomm. Plain IP addresses "
        "and host names work as before; the path is added automatically.",
        "Behind a proxy, the station sees the real caller's address (X-Real-IP / X-Forwarded-For).",
        "Clear messages for proxy problems (station unreachable, HTTPS redirect, upload limit) and "
        "for stations that need updating.",
        "The Add Station dialog explains why an address isn't accepted.",
        "Update every station: this version accepts calls from older ones but can't call them.",
    ]),
    ("0.0.5", "2026-09-27", [
        "Click or tap the clock to open a month calendar over the clock and weather. "
        "Close it with ✕ or Esc; swipe or use the arrows to change the month.",
        "A 7-day strip under the date shows today and the next six days, with today highlighted.",
        "Clearer Time format setting (24-hour or 12-hour) in Preferences → Clock; "
        "AM/PM is shown smaller next to the time.",
        "The clock and weather fit better in narrow windows.",
        "Calendars start the week on your locale's first day (e.g. Sunday in the US, Monday in the UK).",
    ]),
    ("0.0.4", "2026-09-27", [
        "New make_executable.sh makes the install and uninstall scripts executable, "
        "e.g. after copying the folder from Windows or a USB stick.",
    ]),
    ("0.0.3", "2026-09-27", [
        "Other apps (music, videos) are muted while someone is talking to you, "
        "and unmuted afterwards. Switch it off in Preferences → Incoming Calls.",
        "Runs on Raspberry Pi OS Bookworm and Debian 12 (libadwaita 1.2, GTK 4.8).",
        "Installers for Arch Linux and CachyOS.",
        "If the window can't open, LinuxComm quits instead of running invisibly, and "
        "errors are logged to ~/.local/state/linuxcomm/linuxcomm.log.",
        "The installer checks library versions and refuses systems that are too old.",
        "Version history in About → What's New.",
    ]),
]


def release_notes(version: str) -> str:
    """The changes in `version` as AppStream markup, for Adw.AboutDialog/AboutWindow."""
    for release_version, _date, changes in RELEASES:
        if release_version == version:
            return "<ul>" + "".join(f"<li>{escape(change, quote=False)}</li>" for change in changes) + "</ul>"
    return ""
