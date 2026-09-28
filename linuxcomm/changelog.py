"""Version history, shown in About → What's New.

Newest release first. The first entry's version must equal linuxcomm.__version__
(tests/test_version.py checks this).
"""

from __future__ import annotations

from html import escape

# (version, date, changes)
RELEASES: list[tuple[str, str, list[str]]] = [
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
