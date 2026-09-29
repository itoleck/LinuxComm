"""Persistent settings, stored as JSON in ~/.config/linuxcomm/config.json."""

from __future__ import annotations

import copy
import json
import logging
import os
import socket
import threading
import uuid
from pathlib import Path

from . import themes

log = logging.getLogger(__name__)

DEFAULTS: dict = {
    "station_name": "",          # empty = use the host name
    "network_key": "",           # empty = accept calls from anyone
    "do_not_disturb": False,
    "chime": True,
    "feedback_suppression": True,  # notch out howling in the microphone while talking
    "echo_cancellation": True,     # remove what the speaker plays from the microphone (needs webrtcdsp)
    "tts_relay": False,            # talk with a synthetic voice: only text-to-speech audio is sent
    "tts_voice": "",               # "coqui:p225", "pyttsx3:gmw/en-US", ...; empty = the default voice
    "mute_other_apps": True,     # silence other applications while receiving
    "incoming_volume": 100,      # percent
    "input_device": "",          # empty = system default
    "input_device_name": "",
    "output_device": "",
    "output_device_name": "",
    "location": "",              # empty = detect from the public IP address
    "location_cache": None,      # {"query", "name", "latitude", "longitude"}
    "units": "celsius",          # or "fahrenheit"
    "theme": "classic",          # see themes.THEMES
    "text_color": "#000000",     # text in the main window, "#rrggbb"
    "background_image": "",      # a file name in ~/linuxcomm/data/images, or a path; empty = none
    "background_strength": 70,   # percent: how strongly the image shows through the theme's background
    "clock_24h": True,
    "clock_seconds": True,
    "stt_enabled": True,         # captions of incoming speech (when speech-to-text is installed)
    "stt_model": "",             # folder of the Vosk model; empty = the first one found
    "caption_font": "",          # font family; empty = the desktop's font
    "caption_size": 18,          # points
    "transcript_folder": "",     # where Save writes transcripts; empty = ~/linuxcomm/data
    "peers": [],                 # [{"id", "name", "address"}]
}


def _default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / "linuxcomm" / "config.json"


def data_dir() -> Path:
    """LinuxComm's working folder for the user's files: ~/linuxcomm/data."""
    return Path.home() / "linuxcomm" / "data"


def state_dir() -> Path:
    """~/.local/state/linuxcomm: logs and other runtime state."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "linuxcomm"


class Config:
    """Thread-safe settings store; every change is written to disk immediately."""

    def __init__(self, path: Path | None = None):
        self.path = path or _default_path()
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULTS)
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                stored = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("Ignoring unreadable settings file %s: %s", self.path, e)
            return
        if isinstance(stored, dict):
            for key in DEFAULTS:
                if key in stored:
                    self._data[key] = stored[key]
        if self._data.get("transcript_folder") == "/data":
            self._data["transcript_folder"] = ""  # version 0.0.8's default; now ~/linuxcomm/data
        color = themes.parse_hex(self._data.get("text_color"))
        if (isinstance(stored, dict) and "text_color" not in stored
                and themes.is_dark(themes.get(str(self._data.get("theme"))))):
            color = themes.TEXT_COLORS["White"]  # before the setting existed, dark themes had light text
        self._data["text_color"] = color or DEFAULTS["text_color"]
        peers = []
        for p in self._data.get("peers") or []:
            if isinstance(p, dict) and str(p.get("address", "")).strip():
                address = str(p["address"]).strip()
                peers.append({
                    "id": str(p.get("id") or uuid.uuid4().hex),
                    "name": str(p.get("name") or address),
                    "address": address,
                })
        self._data["peers"] = peers

    def save(self) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, indent=2)
                os.replace(tmp, self.path)
            except OSError as e:
                log.error("Could not save settings to %s: %s", self.path, e)

    def get(self, key: str):
        with self._lock:
            return copy.deepcopy(self._data[key])

    def set(self, key: str, value) -> None:
        with self._lock:
            if self._data.get(key) == value:
                return
            self._data[key] = value
            self.save()

    @property
    def station_name(self) -> str:
        return str(self.get("station_name") or "").strip() or socket.gethostname()

    # -- intercom peers -------------------------------------------------------

    def peers(self) -> list[dict]:
        return self.get("peers")

    def find_peer(self, peer_id: str) -> dict | None:
        return next((p for p in self.peers() if p["id"] == peer_id), None)

    def add_peer(self, name: str, address: str) -> dict:
        peer = {"id": uuid.uuid4().hex, "name": name.strip() or address.strip(), "address": address.strip()}
        with self._lock:
            self._data["peers"].append(peer)
            self.save()
        return dict(peer)

    def update_peer(self, peer_id: str, name: str, address: str) -> None:
        with self._lock:
            for p in self._data["peers"]:
                if p["id"] == peer_id:
                    p["name"] = name.strip() or address.strip()
                    p["address"] = address.strip()
            self.save()

    def remove_peer(self, peer_id: str) -> None:
        with self._lock:
            self._data["peers"] = [p for p in self._data["peers"] if p["id"] != peer_id]
            self.save()
