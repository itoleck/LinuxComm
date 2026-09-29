# LinuxComm

A clock, weather display and intercom for Linux desktops on the same network.
Every computer running LinuxComm is a **station**. Stations talk to each other
over plain **HTTP on port 80**. Talk to one station, or to all of them at once, or send a text.

- Large clock and date (12- or 24-hour, optional seconds) with a 7-day strip showing today and the next six days
- Click or tap the clock for a **month calendar**, shown over the clock and weather until you close it
- Three daily **alarms** above the time, with snooze, which other stations can also set remotely
- Current weather, today's high/low and a 4-day forecast ([Open-Meteo](https://open-meteo.com), no API key needed)
- Station list by **IP address or host name**, with a live status for each (online, offline, do not disturb)
- **Talk** to one station, or **Talk to all stations** at once
- **Text messages**: shown across the whole window on the other station, answered with **Reply** by keyboard, on-screen keyboard or dictation
- **Audio device picker** for microphone and speaker, with a mic level meter and a speaker test
- Incoming-call banner with **Hang Up** and **Reply**, an optional chime, desktop notifications and **Do not disturb**
- **Other apps are muted** on the receiving computer while someone is talking to it (music, videos…), then unmuted afterwards
- **Acoustic feedback suppression** removes howling from your microphone's sound while you talk
- **Echo cancellation** keeps what your own speaker plays out of your microphone while you talk
- **Talk with a synthetic voice** (text to speech): record what you say, and a voice reading it is sent as one message, so there is no echo or feedback at all
- **Live captions** of what other stations say, with the whole conversation one click away (speech to text, runs offline)
- Optional shared **network key** so only your stations can talk to each other
- Seven **color themes**, including a dark one, a **text color** for the headings on the window background (black, white or any hex color), and an optional **background image** (downloadable from a URL)

Built with Python 3, GTK 4 / libadwaita and GStreamer. Everything comes from the
distribution's own repositories (Ubuntu, Arch Linux, CachyOS), so there is nothing to compile.
Ubuntu and Arch stations work together on the same network.

## Install (Ubuntu, Debian, Raspberry Pi OS)

Works on Ubuntu 23.04 or newer (26.04 recommended), Debian 12 or newer, and Raspberry Pi OS
Bookworm or Trixie (Debian 12 or 13, desktop edition, 64-bit or 32-bit). Every feature, including
captions and echo cancellation, works on Raspberry Pi OS Trixie and its Python 3.13. LinuxComm needs
libadwaita 1.2 and GTK 4.8 or newer.
On older libadwaita versions, such as Debian 12 and Raspberry Pi OS Bookworm, dialogs open as
separate windows but everything else works the same.

Copy this folder to the machine and run:

```sh
sudo bash install.sh
```

To run the scripts directly (`sudo ./install.sh`), make them executable first with
`bash make_executable.sh`. Copying the folder from Windows, a USB stick or a zip file usually
removes that permission.

The installer usually takes a few seconds (longer if packages need downloading). It:

1. installs any missing packages (`python3-gi`, `gir1.2-gtk-4.0`, `gir1.2-adw-1`, GStreamer, and `pulseaudio-utils`
   for muting other apps; this is just the `pactl` tool, it works with PipeWire and does not replace it),
   and, if it can, `gstreamer1.0-plugins-bad` for [echo cancellation](#echo-cancellation),
2. copies the app to `/opt/linuxcomm` and adds a `linuxcomm` command and an app-grid entry,
3. sets up [captions](#captions-speech-to-text): Vosk in a private Python environment in
   `/opt/linuxcomm/venv` and a small English speech model (a ~40 MB download, ~110 MB on disk),
   and the voices for [talking with a synthetic voice](#talking-with-a-synthetic-voice-text-to-speech):
   `espeak-ng` and pyttsx3 (a few MB), plus Coqui TTS's natural voices on 64-bit systems with 4 GB of
   memory and 4 GB of free disk space (about 1 GB to download, ~2 GB on disk, a few minutes),
4. allows regular users to listen on port 80 (`net.ipv4.ip_unprivileged_port_start=80` in `/etc/sysctl.d/60-linuxcomm.conf`),
5. opens TCP port 80 in `ufw` if the firewall is enabled.

Repeat on every machine that should be a station. Useful options:

```sh
sudo bash install.sh --autostart               # start LinuxComm at login
sudo bash install.sh --autostart --fullscreen  # ...fullscreen, e.g. for a wall-mounted screen
sudo bash install.sh --no-autostart            # stop starting it at login
sudo bash install.sh --no-speech               # without captions and synthetic voices (removes them)
sudo bash install.sh --no-coqui                # without the natural voices (removes them if installed)
```

After upgrading the system, e.g. Raspberry Pi OS from Bookworm to Trixie, run the installer again: it
rebuilds the speech-to-text environment for the new Python version.

To remove it, run `sudo bash uninstall.sh`. Your settings in `~/.config/linuxcomm` and saved
transcripts in `~/linuxcomm/data` are kept.

## Install (Arch Linux / CachyOS)

Copy this folder to the machine and run:

```sh
sudo bash install_arch.sh
```

As on Ubuntu, `bash make_executable.sh` lets you run it as `sudo ./install_arch.sh` instead.

It takes the same options as the Ubuntu installer (`--autostart`, `--fullscreen`, `--no-autostart`,
`--no-speech`, `--no-coqui`), sets up captions and the synthetic voices the same way, and:

1. installs any missing packages with `pacman -S --needed` (`python-gobject`, `gtk4`, `libadwaita`,
   GStreamer, `libpulse` for `pactl`; plus `gst-plugin-pipewire` on PipeWire systems and `noto-fonts-emoji` if no emoji font is installed;
   `gst-plugins-bad` for [echo cancellation](#echo-cancellation) if it can),
2. copies the app to `/opt/linuxcomm`, adds a `linuxcomm` command, and puts the menu entry and icon
   in `/usr/local/share` (pacman owns `/usr/share`),
3. allows regular users to listen on port 80 (same sysctl setting as on Ubuntu),
4. opens TCP port 80 in `ufw` (enabled by default on CachyOS) or `firewalld`, whichever is active.

The installer never runs `pacman -Sy`, because partial upgrades break Arch systems. If package
installation fails because the package database is out of date, run `sudo pacman -Syu` and then run
the installer again. If you use `doas` instead of `sudo`, the scripts work with that too.

To remove it, run `sudo bash uninstall_arch.sh`. The uninstaller leaves the packages installed
because other applications use them.

`hostname.local` names only work on Arch if Avahi and `nss-mdns` are set up; otherwise use IP addresses.

## Using it

1. Click **+** next to *Intercom* and enter the other station's IP address or host name.
   The window's subtitle shows this station's own IP address. On most home networks,
   `hostname.local` works too. For a station behind a reverse proxy, enter its URL, such as
   `http://swarmsoft.com/linuxcomm` (see [Behind a reverse proxy](#behind-a-reverse-proxy-nginx)).
2. The dot next to each station shows its status: green = online, orange = do not disturb,
   grey = offline or not running LinuxComm.
3. Click **Talk** to speak to one station, or **Talk to all stations** to speak to every station.
   Click again to stop. Talking stops automatically after 5 minutes in case the microphone was left on.
4. When someone talks to you, a banner shows who it is, with **Hang Up** and **Reply**.
   While they talk, you can't start a talk of your own (the **Talk** buttons and **Talk to all stations**
   are greyed out), because your microphone would send their voice straight back to them and
   howl. A talk you had already started keeps going and can still be stopped.
   - **Reply** ends every current talk (theirs, anyone else's, and yours) and talks to the caller.
     They see "Living Room is replying", and your voice follows a moment later.
   - **Hang Up** ends the incoming call: its sound stops at once and the caller sees
     "Living Room hung up". After that you can talk again.
   While they talk, other apps on your computer are muted. They are unmuted 1.5 seconds after
   the last incoming audio, so music doesn't come back between one person talking and the other replying.
   Only the apps LinuxComm muted are unmuted; anything you muted yourself stays muted.
5. Choose the microphone and speaker under **Audio devices**. **Test** shows the mic level or plays the chime.
6. Click or tap the clock to open the month calendar; it covers the clock and weather, while the
   intercom stays usable. Change months with the arrows (or swipe on a touchscreen), jump back with
   **Today**, and close it with ✕ or <kbd>Esc</kbd>. Both calendars mark today in the accent color and
   start the week on your locale's first day (Sunday in the US, Monday in the UK and most of Europe).
7. When someone talks to you, the **captions bar** at the bottom of the window shows their last two
   sentences as they speak. Click or tap it to see everything said in the current conversation, in
   the calendar's place, and **Save** it to a text file. See [Captions](#captions-speech-to-text).
8. Click or tap **Alarm 1**, **Alarm 2** or **Alarm 3** above the time to set an alarm, and the bell
   next to a station's **Talk** button to set that station's alarms. See [Alarms](#alarms).
9. Click **Text** next to a station's **Talk** button to send it a message. See [Text messages](#text-messages).

Shortcuts: <kbd>Ctrl</kbd>+<kbd>T</kbd> talk to all · <kbd>F11</kbd> fullscreen ·
<kbd>Ctrl</kbd>+<kbd>,</kbd> preferences · <kbd>Ctrl</kbd>+<kbd>Q</kbd> quit.

**Preferences** (menu, top right): color theme, text color, background image, station name, network key, acoustic feedback suppression, echo cancellation, synthetic voice, chime, muting other apps,
incoming volume, captions (on/off, language model, font, size, where transcripts are saved), time format
(24-hour or 12-hour), seconds, weather location (empty = detect from your internet connection; `Berlin`,
`Portland, OR` and `47.61, -122.33` all work) and units.

LinuxComm only answers calls while it is running, so use `--autostart` on dedicated intercom machines.

### Themes

Preferences → *Appearance* → *Theme* recolors the whole window right away:

| Theme | Background | Surface / cards | Primary (buttons) | Accent | Text | Secondary text | Borders | Today |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Classic (default) | `#FFFFFF` | `#F8F9FA` | `#2563EB` | `#3B82F6` | `#111827` | `#6B7280` | `#E5E7EB` | `#EF4444` |
| Soft Dark | `#0F172A` | `#1E293B` | `#60A5FA` | `#38BDF8` | `#F1F5F9` | `#94A3B8` | `#334155` | `#F87171` |
| Forest | `#F0FDF4` | `#FFFFFF` | `#166534` | `#22C55E` | `#14532D` | `#4B5563` | `#BBF7D0` | `#F59E0B` |
| Ocean | `#F0F9FF` | `#FFFFFF` | `#0369A1` | `#0EA5E9` | `#0C4A6E` | `#64748B` | `#BAE6FD` | `#F97316` |
| Sunset | `#FFF7ED` | `#FFFFFF` | `#C2410C` | `#F97316` | `#9A3412` | `#78716C` | `#FED7AA` | `#DC2626` |
| Monochrome | `#FAFAFA` | `#FFFFFF` | `#171717` | `#525252` | `#0A0A0A` | `#737373` | `#E5E5E5` | `#DC2626` |
| Lavender | `#FAF5FF` | `#FFFFFF` | `#7C3AED` | `#A78BFA` | `#4C1D95` | `#6B7280` | `#E9D5FF` | `#EC4899` |

*Primary* colors buttons such as **Talk to all stations**, switches and selections; *Accent* colors
highlights such as the dot of a station you are talking with; *Today* marks today in the 7-day strip
and the month calendar. Text on colored backgrounds is black or white, whichever reads better.
**Stop talking** is always the same red, whatever the theme. The themes are defined in
`linuxcomm/themes.py`; adding one there adds it to the list.

### Text color

Preferences → *Appearance* → *Text color* sets the color of the text that sits directly on the
window background: the *Intercom* and *Audio devices* headings, the line under *Intercom* ("Click
Talk to speak to a station…"), and the "Talking to …" line shown while you talk. Useful with a
background image, where the theme's text color may not stand out. **Black** (`#000000`) is the
default and **White** (`#ffffff`) is one click away. For any other color, type an HTML hex code such as `#1a2b3c` (or
`#abc`) in *Text color as hex* and press Enter or ✓, or click the color swatch next to it to pick one.

- Buttons, and everything in the cards and panels (the clock, weather, station list, audio devices,
  calendar, captions, text messages) keep the theme's colors, as do Preferences, dialogs and menus.
- Choosing a dark theme (Soft Dark) switches black text to white, and a light theme switches white text
  back to black. Other colors are kept. When the chosen color is hard to read on the theme's background
  color, the *Text color* row says so. Over a background image, choose whichever reads better on it.
- Settings from before this option existed keep white text on Soft Dark.

### Background image

Preferences → *Appearance* can put a picture behind the whole window. The cards stay solid on top of
it, and the header bar lets it show through a little.

- **Download an image from a URL:** paste the address of an image (`http://` or `https://`) and press
  **Download** (or Enter). It is saved in `~/linuxcomm/data/images/` under the name from the URL,
  e.g. `sunset.jpg` (or `sunset-2.jpg` if that name is taken), and becomes the background. Only images
  up to 30 MB are accepted, and a web page instead of an image is refused.
- **Background image (file name or path):** type the name of an image in `~/linuxcomm/data/images/`
  (e.g. `sunset.jpg`), or a full path such as `/usr/share/backgrounds/…`, and press ✓. Copy pictures
  into that folder to use them by name. ✕ removes the background.
- **Image strength** (10–100 %, default 70 %) sets how strongly the picture shows through the theme's
  background color. Lower it if headings like *Intercom* are hard to read over a busy picture, or change the [text color](#text-color).

JPEG, PNG and the other formats GTK can open are supported. The image scales to fill the window.
The log (in the terminal and `~/.local/state/linuxcomm/linuxcomm.log`) shows the full path of the
image in use, e.g. `INFO linuxcomm: Loaded background image /home/pi/linuxcomm/data/images/sunset.jpg`,
and for a download, the URL it came from.

## Alarms

The three cells above the time are **Alarm 1**, **Alarm 2** and **Alarm 3**. Each shows its time,
*Off*, *Not set*, *Snoozed · 7:40 AM* or *Ringing*.

- **Set an alarm:** click or tap its cell. The alarm settings open in the calendar's place on the left
  (the intercom stays usable). Pick the time to the minute with + and − (hours follow the 12- or
  24-hour setting), leave **Ring every day** on, and press **Create alarm** or **Save**. **Delete**
  removes it. The buttons at the top switch between Alarm 1, 2 and 3.
- **Every day:** an alarm that is on rings every day at its time. One that is off keeps its time but
  doesn't ring. An alarm created during its own minute (e.g. at 7:30 for 7:30) first rings tomorrow.
- **Saved:** alarms are written to `~/linuxcomm/data/alarms.json` the moment they change, so they are
  kept when LinuxComm restarts.
- **Ringing:** the left panel shows the alarm with **Snooze 10 minutes** and **Dismiss**, and an alarm
  tone plays on the chosen speaker. Snoozing rings again 10 minutes later without changing the saved
  alarm. Other left-side panels can't replace the ringing panel until you choose, and an alarm nobody
  answers stops by itself after 15 minutes. If the computer was busy, an alarm up to 5 minutes late
  still rings; after a longer sleep it waits for the next day.
- **Another station's alarms:** the bell next to a station's **Talk** button opens that station's
  alarms in the same panel; changes are saved on that station, which shows who changed them. The other
  station must run LinuxComm 0.0.12 or newer, and must have the same network key if one is set.

## Acoustic feedback suppression

When two stations are close enough that one's speaker is heard by the other's microphone, your
voice can come back around and build up into a loud howl (the Larsen effect). LinuxComm watches the
sound of your microphone while you talk, 20 times a second. Howling looks different from speech: a
single frequency that stands far above everything around it and stays put. When it finds one, a
narrow notch filter removes just that frequency, usually within a third of a second, and cuts deeper
(up to 24 dB) if the howl persists. Up to eight howling frequencies are handled at once. Speech
passes through unchanged, and the filters start fresh with every talk.

- It is **on by default**. Preferences → *Talking* → *Acoustic feedback suppression* turns it off
  or on, which also takes effect in the middle of a talk.
- While you talk, the status line reads "Talking to Kitchen · 0:12 · Feedback suppressed" once it
  has acted, and the log names the frequency, e.g.
  `INFO linuxcomm.audio: Acoustic feedback at 1234 Hz: notch filter at -15 dB`.
- It uses the `spectrum` and `equalizer-nbands` elements from GStreamer's good plugins, which the
  installers already install. Without them, talking works as before and the log says the suppression
  is not available.
- A held whistle or a steady tone can look like a howl and get notched too; it is only removed for
  the rest of that talk.
- It deals with howling. What your own speaker plays is handled by [echo cancellation](#echo-cancellation).
  Stations that are very close together still work best with headsets or a lower volume.

## Echo cancellation

While you talk, anything this computer's speaker plays would otherwise be picked up by your
microphone and sent along with your voice: another station talking to you while you talk to someone
else, the last words of a caller when you press **Reply**, the chime, or music and videos from other
apps. Echo cancellation removes it, using the same WebRTC audio processing as web browsers:

- LinuxComm records what the chosen speaker plays (its *monitor*, from PulseAudio or PipeWire) and
  subtracts it from the microphone. Your voice passes unchanged; noise suppression and automatic gain
  are not used.
- The delay between the speaker and the microphone depends on the hardware. The WebRTC audio
  processing of Raspberry Pi OS Trixie, Debian 13, Ubuntu 25.10 and newer, and Arch finds it by
  itself. The older one of Raspberry Pi OS Bookworm and Debian 12 doesn't, so there LinuxComm measures
  it during the first seconds a sound plays and adjusts; the log then says e.g.
  `Echo cancellation: sound from Speakers comes back into the microphone after about 60 ms`.
  When a talk starts, the log says which applies.
- It is **on by default** where available. Preferences → *Talking* → *Echo cancellation* turns it off
  or on; the change takes effect from the next talk.
- It needs GStreamer's `webrtcdsp`, from `gstreamer1.0-plugins-bad` (Debian 12 and 13, Raspberry Pi
  OS Bookworm and Trixie, Ubuntu 25.10 and newer) or `gst-plugins-bad` (Arch, CachyOS), which the
  installers add. Ubuntu 24.04's package leaves `webrtcdsp` out, so there the switch is greyed out and
  LinuxComm works without it.
- It handles this computer's own speaker. When two stations are within earshot of each other, the
  sound that comes back goes through the *other* station's speaker; that case is covered by the rule
  that you can't talk while someone talks to you, and by
  [acoustic feedback suppression](#acoustic-feedback-suppression).

## Talking with a synthetic voice (text to speech)

Preferences → *Talking* → **Text-to-speech voice (TTS)** turns talking into sending a spoken message:

1. Press **Talk**: LinuxComm records, and the button turns into **Send** (**Send to all stations** for
   *Talk to all stations*). Nothing is sent, and the other station isn't even called; the status line
   shows what speech to text recognizes as you speak, e.g. "Recording a message for Kitchen · 0:05 ·
   “Dinner is ready.”".
2. Press **Send** when you're done. Everything recognized is read out by the synthetic voice and
   sent as **one message**. The status line follows it ("Preparing the voice…", "Sending to
   Kitchen…"), and then LinuxComm says "Sent to Kitchen: “Dinner is ready.”". If nothing was
   recognized, nothing is sent. You can record the next message while one is still being sent.

Your microphone's sound is never sent, only the synthetic voice: no background noise and no other
station's speaker, so there can be no echo or feedback, whatever the setup.

- **Timing:** the message goes out a moment after you press Talk again: on a PC, a second or two to
  prepare it, plus the time it takes to say it. On a Raspberry Pi, natural voices take longer.
- **What is said** is what speech to text recognized, so speak clearly and check the status line before
  pressing Send. The words come from the speech model's language (English by default).
- **Voice:** choose one under *Voice* (the chosen voice's full name is shown under it), and press ▶ to
  hear it on this computer. Type in the list to search it, e.g. "english" (libadwaita 1.4 and newer).
  - **Natural voice … · Coqui**: 109 natural-sounding English voices from [Coqui TTS](https://github.com/idiap/coqui-ai-TTS)
    (the maintained `coqui-tts`). The installer adds them on 64-bit systems with at least 4 GB of memory
    and 4 GB of free disk space: about 1 GB to download and 2 GB on disk, and about 1 GB of memory while
    the setting is on. A PC speaks a sentence in a fraction of its length; a Raspberry Pi 5 should
    manage about real time and a Pi 4 is slower than real time (estimates: not measured on a Pi).
    Not available on 32-bit systems (PyTorch doesn't exist there).
  - **… · eSpeak**: 141 robotic but instant voices in many languages, from eSpeak NG through
    [pyttsx3](https://pypi.org/project/pyttsx3/). Always installed; used whenever Coqui isn't installed
    or doesn't start (LinuxComm then says so).
- The voices run in a separate process in `/opt/linuxcomm/venv`, started in the background when the
  setting is turned on, so the first talk isn't delayed; turning it off frees their memory.
  `sudo bash install.sh --no-coqui` leaves out, or removes, the natural voices.
- It needs speech to text (the installer sets it up; captions themselves can stay off).

## Text messages

- **Send:** click **Text** next to a station's **Talk** button. Type the message and press
  <kbd>Enter</kbd> or **Send** (<kbd>Shift</kbd>+<kbd>Enter</kbd> starts a new line). Messages can be
  up to 1000 characters. If the station can't be reached, the message stays in the box to try again.
- **Receive:** an incoming text covers the whole LinuxComm window, header included: a band across the
  full width, centered top to bottom, reading *Text from: Kitchen*, then the message, with **Reply** and
  the time and date at the bottom right. The chime plays if it is on. Click or tap anywhere on it (or
  press <kbd>Esc</kbd>) to close it. Texts that arrive meanwhile wait their turn ("2 more messages").
  If LinuxComm is not the active window, it asks to be brought to the front and shows a desktop
  notification. Wayland desktops (Ubuntu, Raspberry Pi OS) don't let an app put itself above other
  apps' windows, so there the notification is what gets your attention; in fullscreen or kiosk use
  LinuxComm is already in front.
- **Reply** opens the same message box addressed to the sender. Type with a keyboard, with
  **Keyboard** (a built-in on-screen keyboard for touchscreens: <kbd>⇧</kbd> for one capital letter,
  **?123** for numbers and symbols), or press **Dictate** and speak: the words appear in the box as
  they are recognized, using the same offline speech recognition as [captions](#captions-speech-to-text)
  (it works even when captions are turned off). Press **Dictate** again to stop, or just press **Send**;
  it waits for the last words. All three can be mixed in one message.
- **Do not disturb** refuses texts as well as calls; the sender sees "Do not disturb is on".
- The other station must run LinuxComm 0.0.13 or newer, and must have the same network key if one is set.

## Captions (speech to text)

Incoming speech is turned into text on the receiving station with [Vosk](https://alphacephei.com/vosk),
an offline speech recognizer. Nothing is sent anywhere.

- **The captions bar** shows the last two sentences, newest at the bottom. Words appear about half a
  second after they are spoken (in italics) and settle into a sentence when the speaker pauses.
  Only what other stations say is captioned, not your own speech.
- **The transcript** opens when you click or tap the bar. It takes the calendar's place on the left,
  covering the clock and weather while the intercom stays usable, and lists everything said in the
  current conversation with times and speaker names. During a call it updates live, including the
  sentence still being spoken (in italics), and follows new text unless you scroll up. Close it with
  ✕, <kbd>Esc</kbd> or another click on the bar. A conversation ends after 10 minutes without incoming
  speech; the next call starts a new transcript.
- **Save** writes the transcript to a text file in LinuxComm's working folder, `~/linuxcomm/data`
  (e.g. `/home/pi/linuxcomm/data/`; it is created on the first save). The file is named with the
  date and time the conversation started and who spoke, e.g. `2026-09-28_14-05-33_Kitchen.txt` or
  `2026-09-28_14-05-33_Kitchen_Office.txt`. Saving again during the same conversation updates that
  file. Only finished sentences are saved. Nothing is saved unless you press Save; otherwise
  transcripts only live in memory. **Copy** puts the transcript on the clipboard; **Clear** empties it.
- **Settings:** Preferences → *Captions*: turn speech to text on or off (off also frees the memory the
  model uses), choose the font and the size, pick the language model if several are installed, and
  change where transcripts are saved (*Save transcripts to*, `~/linuxcomm/data` by default).
- **Accuracy:** the small English model is designed for small devices such as the Raspberry Pi, and is
  good with clear speech. It doesn't punctuate, so every sentence ends with a period, including questions, and it can
  get names and unusual words wrong.
- **Other languages or a bigger model:** download one from the
  [Vosk models page](https://alphacephei.com/vosk/models), unzip it into `~/.local/share/linuxcomm/models/`
  (or `/opt/linuxcomm/models/` for every user), and choose it under Preferences → *Captions* →
  *Language model*. The larger English models are more accurate but need much more memory.

## How the intercom works

Each station runs a small HTTP server on port 80. Everything is under the path `/linuxcomm`,
which is the same on every station, so you never type it when adding a station:

| Request | Purpose |
| --- | --- |
| `GET /linuxcomm/` | Human-readable status page. Open `http://<station>/linuxcomm/` in a browser to check a station is reachable. |
| `GET /linuxcomm/api/status` | JSON: station name, do-not-disturb state. Polled every 10 s to show the status dots. |
| `POST /linuxcomm/api/ring` | Asks whether the station accepts a call (network key and do-not-disturb check). |
| `POST /linuxcomm/api/talk` | The live audio. A chunked request body of 16 kHz mono 16-bit PCM (32 KB/s), played as it arrives. A station that hangs up answers early with 409 `{"hangup": "hangup"}` (or `"reply"`), and the caller stops sending. |
| `GET /linuxcomm/api/alarms` | The station's alarms: `{"alarms": {"1": {"time": "07:30", "enabled": true}, "2": null, "3": null}}`. |
| `PUT /linuxcomm/api/alarms/<1-3>` | Create or change an alarm: `{"time": "07:30", "enabled": true}`. |
| `DELETE /linuxcomm/api/alarms/<1-3>` | Delete an alarm. |
| `POST /linuxcomm/api/text` | Show a text message: `{"text": "Dinner is ready"}` (up to 1000 characters). Refused with 409 while do not disturb is on. |

Audio is played with an 80 ms jitter buffer, so it arrives roughly a tenth of a second after it is spoken.
**Talk to all** opens one stream per station, so a slow or unreachable station never
delays the others. Several people can talk to the same station at once; their audio is mixed.
Hang Up and Reply tell a caller running 0.0.17 or newer what happened; an older caller just sees the
connection drop.

Stations before version 0.0.6 used the same paths without `/linuxcomm`. Newer stations still
accept calls from them, but can't call them (the station list says "Runs an older LinuxComm"),
so update every station.

## Behind a reverse proxy (nginx)

Because the path is always `/linuxcomm`, a web server can forward it to a station. The station
can then be added as `http://swarmsoft.com/linuxcomm`, or simply `swarmsoft.com`, since the path
is added automatically. In the nginx site that listens on port 80 for that name, add:

```nginx
location /linuxcomm/ {
    proxy_pass http://192.168.1.20;      # the LinuxComm station; no path, so /linuxcomm/ is kept
    proxy_http_version 1.1;              # required to stream the talk audio through
    proxy_request_buffering off;         # forward audio as it arrives, not after the speaker stops
    client_max_body_size 0;              # no size limit; the default 1 MB cuts a talk off after ~30 s
    proxy_set_header X-Real-IP $remote_addr;                      # show the real caller's address
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

Then reload nginx (`sudo nginx -t && sudo systemctl reload nginx`). Without
`proxy_request_buffering off`, nginx holds all the audio until the speaker stops talking and
then delivers it in one burst.

- **Plain HTTP only:** LinuxComm talks plain HTTP. If the site redirects HTTP to HTTPS, leave
  this location out of the redirect; otherwise the station list shows "Redirected (to HTTPS?)".
- **One station per host name:** the path can't be changed, so each host name (or port) reaches
  one station. Use e.g. `kitchen.swarmsoft.com` and `office.swarmsoft.com` for more.
- **Replies:** the station behind nginx sees the caller's real address through `X-Real-IP`.
  If that address can't be reached directly, **Reply** uses the caller's entry in the station
  list instead, matched by station name, so add each other on both sides.
- **Firewall:** port 80 must be open on the nginx server. The station itself only needs to
  accept connections from nginx.

## Security

- Traffic is **unencrypted HTTP**, as required for port 80. Use LinuxComm on a network you trust.
- With a **network key** set (it must match on every station), each request carries an
  HMAC-SHA256 signature and a timestamp. The key itself is never sent over the network,
  and stations without the key are refused. The audio itself is still unencrypted.
  Stations' clocks must be within 5 minutes of each other (Ubuntu syncs time automatically).
- Lowering `ip_unprivileged_port_start` to 80 lets any local user open ports 80–1023,
  which is fine for a dedicated or single-user machine. The uninstaller restores the default (1024).
- Weather lookups go to `api.open-meteo.com` over HTTPS. If no location is set, the
  approximate location is detected once via `ipinfo.io` (or `ip-api.com` as a fallback).
- Like talking, reading and changing a station's alarms, and sending it texts, is open to anyone on
  the network unless a **network key** is set; with a key, only stations with the same key can do it.
  Texts are sent unencrypted and are not saved anywhere.
- Captions are recognized on the receiving station itself; audio and text never leave it, and
  transcripts are only kept in memory unless you press **Save**. The installer downloads Vosk from
  PyPI and the model from `alphacephei.com`.
- Saved transcripts are ordinary files in your home folder (`~/linuxcomm/data`), with the same
  permissions as the rest of it. Uninstalling LinuxComm doesn't delete them.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| The window doesn't open | Run `linuxcomm` in a terminal on the machine's desktop (not over SSH) to see the error, or read `~/.local/state/linuxcomm/linuxcomm.log`. If a failed older version is still running in the background, stop it with `pkill -f "python3 -m linuxcomm"` first. |
| Banner: "no permission to use port 80" | Run `sudo bash install.sh` (Arch/CachyOS: `install_arch.sh`). It enables port 80 for regular users. |
| Banner: "port 80 is used by another program" | Another web server (Apache, nginx…) is running. Stop it, or use a different machine. |
| Station shows "Not responding" | Check the address, and make sure the firewall on *that* machine allows TCP port 80. The installers handle `ufw` and `firewalld`; other firewalls (e.g. hand-written nftables rules) need port 80 opened by hand. |
| "Something other than LinuxComm answers there" | A different web server is on port 80 at that address. |
| "Runs an older LinuxComm" | That station is on version 0.0.5 or older. Update it. |
| "No LinuxComm station at /linuxcomm there" | The web server at that address doesn't forward `/linuxcomm/`. Add the nginx `location` from [Behind a reverse proxy](#behind-a-reverse-proxy-nginx). |
| "The proxy there can't reach the station" | nginx can't connect to the station: check the `proxy_pass` address and that LinuxComm is running there. |
| "Redirected (to HTTPS?)" | The site redirects to HTTPS. Leave `location /linuxcomm/` out of the redirect. |
| "The proxy there limits upload size" | Add `client_max_body_size 0;` to the nginx `location`. |
| Through nginx, audio arrives in one burst after the speaker stops | Add `proxy_http_version 1.1;` and `proxy_request_buffering off;` to the nginx `location`. |
| "Wrong network key" / "requires a network key" | Set the same key in Preferences on both stations. |
| No sound or wrong device | Pick devices under *Audio devices*, click ↻ after plugging in a headset, then use the **Test** buttons. |
| Weather symbols show as empty boxes | The icon theme has no weather icons and no emoji font is installed. Install one (`noto-fonts-emoji` on Arch, `fonts-noto-color-emoji` on Ubuntu). |
| Other apps aren't muted during calls | Check that *Mute other apps* is on in Preferences and that `pactl` is installed (`pulseaudio-utils`, or `libpulse` on Arch). The log says so if it's missing. |
| An app stayed muted | Unmute it in the system sound settings. LinuxComm also unmutes the apps it muted the next time it starts, even after a crash. |
| No captions bar, or Preferences says speech to text is "Not installed" | Run the installer again with an internet connection; it downloads Vosk and the model. Also check that *Speech to text* is on. |
| "No permission to save in …" | The folder under Preferences → *Captions* → *Save transcripts to* isn't writable for your user. Choose another one, or clear the field to go back to `~/linuxcomm/data`. |
| An alarm didn't ring | Check that it is on (its cell shows the time, not *Off*) and that LinuxComm was running. The alarm tone plays on the speaker chosen under *Audio devices*; use its **Test** button. |
| Setting another station's alarms says "Runs an older LinuxComm" | Update that station to 0.0.12 or newer. |
| Sending a text says "Runs an older LinuxComm" | Update that station to 0.0.13 or newer. |
| **Dictate** is greyed out | Point at it to see why: speech to text or its model isn't installed. Run the installer again with an internet connection. |
| The *Intercom* or *Audio devices* heading is invisible or hard to read | Preferences → *Appearance* → *Text color*: choose **Black**, or **White** on Soft Dark or a dark background image. |
| The background image doesn't appear | Check the name under Preferences → *Appearance*; a bare name must be a file in `~/linuxcomm/data/images/`. If the file was moved or deleted, LinuxComm shows the theme's plain background and logs "Background image … not found". |
| Captions bar says "No speech model is installed" | The model download failed or was deleted. Run the installer again, or add a model as described in [Captions](#captions-speech-to-text). |
| The **Talk** buttons are greyed out | Another station is talking to you. Press **Reply** to answer it, or **Hang Up**, or wait until it finishes. |
| Echo or feedback | Stations near each other pick up each other's speakers. Keep *Acoustic feedback suppression* and *Echo cancellation* on (Preferences → *Talking*), and use headsets or lower the incoming volume if it still happens. |
| *Text-to-speech voice* is greyed out | Speech to text isn't installed. Run the installer again (with an internet connection, and without `--no-speech`). |
| Only eSpeak voices, no natural (Coqui) voices | The installer only adds them on 64-bit systems with 4 GB of memory and disk space, and says why it didn't. If they are installed but don't start, LinuxComm says so; see `~/.local/state/linuxcomm/tts-coqui.log`. |
| The synthetic voice says the wrong words | It reads what speech to text recognized: speak clearly and close to the microphone, and check the status line before pressing Send. |
| *Echo cancellation* is greyed out | GStreamer's `webrtcdsp` is missing. Run the installer again; on Ubuntu 24.04 it isn't packaged, so echo cancellation is not available there. |

Run `linuxcomm --debug` in a terminal for detailed logs. Logs are also written to
`~/.local/state/linuxcomm/linuxcomm.log`.

## Running from source and tests

```sh
python3 -m linuxcomm                                  # from this folder; needs the packages above
LINUXCOMM_PORT=8080 python3 -m linuxcomm              # use another port for testing (peers: host:8080)
python3 -m unittest discover -s tests -v              # protocol, muting and version tests; no GTK or root needed
```

**Versioning:** the version lives in `linuxcomm/__init__.py` and is shown in **About** (also
`linuxcomm --version`, and in the installers' final message). Every change bumps it and adds an
entry at the top of `linuxcomm/changelog.py`, which appears under About → **What's New**.
A test fails if the two don't match.

Layout: `linuxcomm/app.py` (GTK interface), `intercom.py` (HTTP protocol, presence, streaming),
`audio.py` (GStreamer capture/playback, device list), `weather.py` (Open-Meteo), `config.py` (settings),
`muting.py` (muting other apps during calls), `calendar_view.py` and `dates.py` (calendars),
`speech.py` and `captions_view.py` (captions),
`themes.py` (color themes), `backgrounds.py` (background images), `alarms.py` and `alarms_view.py`
(alarms), `changelog.py` (version history), `compat.py` (fallbacks for libadwaita 1.2–1.4 and GTK 4.8–4.11).

Weather data by [Open-Meteo.com](https://open-meteo.com/) (CC BY 4.0).
