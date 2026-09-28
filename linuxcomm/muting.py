"""Mute other applications' audio while intercom audio is playing.

Uses `pactl`, which works with PipeWire (through pipewire-pulse) and PulseAudio.
Only streams that were audible and that this module muted are unmuted again, so
anything the user muted themselves stays muted. The list of muted streams is also
saved to disk, so a crash mid-call is repaired the next time LinuxComm starts.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

RELEASE_DELAY_S = 1.5    # unmute this long after the last incoming audio ends
RESCAN_INTERVAL_S = 1.0  # while muted, look for newly started streams this often


class PactlBackend:
    """Lists and mutes playback streams ("sink inputs") with pactl."""

    @staticmethod
    def _run(*args: str) -> str:
        return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5, check=True).stdout

    def streams(self) -> list[dict]:
        """Return [{"index": int, "pid": str, "app": str, "muted": bool}, ...]."""
        streams = []
        for s in json.loads(self._run("-f", "json", "list", "sink-inputs") or "[]"):
            props = s.get("properties") or {}
            streams.append({
                "index": int(s["index"]),
                "pid": str(props.get("application.process.id", "")),
                "app": str(props.get("application.name", "")),
                "muted": bool(s.get("mute")),
            })
        return streams

    def set_mute(self, index: int, mute: bool) -> None:
        self._run("set-sink-input-mute", str(index), "1" if mute else "0")


_ERRORS = (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError)


class OtherAppsMuter:
    """Call acquire() when incoming audio starts and release() when it ends.

    Both may be called from any thread; the pactl work happens on a background
    thread so it never delays the audio itself.
    """

    def __init__(self, enabled: Callable[[], bool], state_file: Path | None = None, backend=None,
                 release_delay: float = RELEASE_DELAY_S, rescan_interval: float = RESCAN_INTERVAL_S):
        self._enabled = enabled
        self._state_file = state_file
        self._backend = backend or PactlBackend()
        self._release_delay = release_delay
        self._rescan_interval = rescan_interval
        self._own_pid = str(os.getpid())
        self._cond = threading.Condition()
        self._active = 0
        self._release_at: float | None = None
        self._stopped = False
        self._muted: dict[int, str] = {}  # stream index -> pid of its application (worker thread only)
        self._warned = False
        self._thread = threading.Thread(target=self._run, name="mute-other-apps", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def acquire(self) -> None:
        with self._cond:
            self._active += 1
            self._release_at = None
            self._cond.notify()

    def release(self) -> None:
        with self._cond:
            self._active = max(0, self._active - 1)
            if self._active == 0:
                self._release_at = time.monotonic() + self._release_delay
            self._cond.notify()

    def stop(self) -> None:
        """Unmute everything this muter muted and end the worker thread."""
        with self._cond:
            self._stopped = True
            self._cond.notify()
        if self._thread.is_alive():
            self._thread.join(5)

    # -- worker thread -----------------------------------------------------------

    def _run(self) -> None:
        self._muted = self._load_state()
        if self._muted:
            log.info("Unmuting %d stream(s) left muted by a previous run", len(self._muted))
            self._restore()
        while True:
            with self._cond:
                while True:
                    if self._stopped:
                        action = "stop"
                        break
                    if self._active:
                        action = "mute"
                        break
                    now = time.monotonic()
                    if self._release_at is not None and now >= self._release_at:
                        self._release_at = None
                        action = "restore"
                        break
                    self._cond.wait(None if self._release_at is None else self._release_at - now)
            if action == "stop":
                self._restore()
                return
            if action == "restore":
                self._restore()
                continue
            if self._enabled():
                self._mute_others()
            with self._cond:
                if self._active and not self._stopped:
                    self._cond.wait(self._rescan_interval)  # then look for newly started streams

    def _streams(self) -> list[dict] | None:
        try:
            return self._backend.streams()
        except _ERRORS as e:
            if not self._warned:
                self._warned = True
                if isinstance(e, FileNotFoundError):
                    log.warning("Can't mute other apps: pactl is missing (install pulseaudio-utils, or libpulse on Arch)")
                else:
                    log.warning("Can't list audio streams to mute other apps: %s", e)
            return None

    def _mute_others(self) -> None:
        streams = self._streams()
        if streams is None:
            return
        changed = False
        for s in streams:
            if s["pid"] == self._own_pid or s["muted"] or s["index"] in self._muted:
                continue
            try:
                self._backend.set_mute(s["index"], True)
            except _ERRORS as e:
                log.debug("Could not mute stream %s: %s", s["index"], e)
                continue
            self._muted[s["index"]] = s["pid"]
            changed = True
            log.info("Muted %s during intercom audio", s["app"] or f"stream {s['index']}")
        if changed:
            self._save_state()

    def _restore(self) -> None:
        if not self._muted:
            return
        streams = self._streams()
        current = {s["index"]: s for s in streams} if streams is not None else {}
        for index, pid in self._muted.items():
            stream = current.get(index)
            if stream is None or stream["pid"] != pid:
                continue  # the stream ended, or its index now belongs to another application
            try:
                self._backend.set_mute(index, False)
                log.info("Unmuted %s", stream["app"] or f"stream {index}")
            except _ERRORS as e:
                log.debug("Could not unmute stream %s: %s", index, e)
        self._muted.clear()
        self._save_state()

    def _load_state(self) -> dict[int, str]:
        if not self._state_file:
            return {}
        try:
            with open(self._state_file, encoding="utf-8") as f:
                return {int(index): str(pid) for index, pid in json.load(f).items()}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, AttributeError) as e:
            log.debug("Ignoring unreadable %s: %s", self._state_file, e)
            return {}

    def _save_state(self) -> None:
        if not self._state_file:
            return
        try:
            if self._muted:
                self._state_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self._state_file, "w", encoding="utf-8") as f:
                    json.dump({str(i): pid for i, pid in self._muted.items()}, f)
            else:
                self._state_file.unlink(missing_ok=True)
        except OSError as e:
            log.debug("Could not save %s: %s", self._state_file, e)
