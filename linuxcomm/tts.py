"""Talking with a synthetic voice (Preferences → Talking → Text-to-speech voice).

While Talk is on, the microphone goes only to speech to text on this computer; nothing is
sent. When you press Talk again, everything recognized is read out by a synthetic voice and
sent as one message: none of the room's sound is transmitted, so there can be no echo or
feedback.

Voices come from Coqui TTS (natural voices; 64-bit systems with enough memory) or, when it
isn't installed or doesn't start, from pyttsx3 with eSpeak NG. Both run in a worker process
(tts_worker.py) in LinuxComm's private Python environment.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import state_dir

log = logging.getLogger(__name__)

ENGINES = ("coqui", "pyttsx3")                    # preferred first
ENGINE_LABELS = {"coqui": "Coqui", "pyttsx3": "eSpeak"}
START_TIMEOUT_S = 240                             # Coqui loads PyTorch and its model: slow on a Pi
PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)


@dataclass(frozen=True)
class Voice:
    engine: str      # "coqui" or "pyttsx3"
    id: str          # the engine's own name for it
    name: str

    @property
    def key(self) -> str:
        """How the voice is stored in the settings."""
        return f"{self.engine}:{self.id}"

    @property
    def label(self) -> str:
        """"English (America) · eSpeak": the name first, so typing it finds it in the list."""
        return f"{self.name} · {ENGINE_LABELS[self.engine]}"


def worker_python() -> str:
    """The Python with the text-to-speech packages: LinuxComm's private environment."""
    if sys.prefix != sys.base_prefix:  # already running in it
        return sys.executable
    installed = Path("/opt/linuxcomm/venv/bin/python3")
    return str(installed) if installed.exists() else sys.executable


def models_dir() -> Path:
    """Where the installer put the speech models and the voice lists."""
    if os.environ.get("LINUXCOMM_MODELS"):
        return Path(os.environ["LINUXCOMM_MODELS"])
    return Path("/opt/linuxcomm/models")


def _end(proc: subprocess.Popen) -> None:
    """Stop a worker process and close its pipes."""
    for pipe in (proc.stdin, proc.stdout):
        try:
            pipe.close()  # a running worker reads the end of its input and exits
        except OSError:
            pass
    try:
        proc.wait(3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _voices_from(message: dict, engine: str) -> list[Voice]:
    if not message.get("ready"):
        return []
    return [Voice(engine, str(v["id"]), str(v["name"])) for v in message.get("voices", [])]


class Worker:
    """One engine's worker process. say() may be called from any thread."""

    def __init__(self, engine: str):
        self.engine = engine
        self.error = ""
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def command(self, prepare: bool = False) -> list[str]:
        cmd = [worker_python(), "-m", "linuxcomm.tts_worker", self.engine, "--models", str(models_dir())]
        return cmd + ["--prepare"] if prepare else cmd

    def _spawn(self, prepare: bool = False) -> tuple[subprocess.Popen, dict]:
        env = dict(os.environ, PYTHONPATH=PACKAGE_PARENT + os.pathsep + os.environ.get("PYTHONPATH", ""))
        try:
            state_dir().mkdir(parents=True, exist_ok=True)
            errors = open(state_dir() / f"tts-{self.engine}.log", "wb")   # the libraries' own output
        except OSError:
            errors = subprocess.DEVNULL
        proc = subprocess.Popen(self.command(prepare), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=errors, env=env)
        if errors is not subprocess.DEVNULL:
            errors.close()
        timer = threading.Timer(START_TIMEOUT_S, proc.kill)
        timer.start()
        try:
            line = proc.stdout.readline()
        finally:
            timer.cancel()
        try:
            message = json.loads(line)
        except ValueError:
            message = {"ready": False, "error": "the voice didn't start (see tts-%s.log)" % self.engine}
        return proc, message

    def list_voices(self) -> tuple[list[Voice], str]:
        """Start the engine once just to list its voices; (voices, error)."""
        proc, message = self._spawn(prepare=True)
        _end(proc)
        return _voices_from(message, self.engine), str(message.get("error", ""))

    def start(self) -> bool:
        """Start the worker if it isn't running (slow for Coqui). False if it can't be used."""
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return True
            started = time.monotonic()
            proc, message = self._spawn()
            if not message.get("ready"):
                self.error = str(message.get("error", "the voice didn't start"))
                _end(proc)
                return False
            self._proc, self.error = proc, ""
            log.info("Text to speech: %s is ready (%.1f s)", ENGINE_LABELS[self.engine], time.monotonic() - started)
            return True

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def say(self, text: str, voice_id: str) -> tuple[int, bytes]:
        """Synthesize `text`; returns (sample rate, 16-bit mono PCM)."""
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                raise RuntimeError(self.error or "the voice isn't running")
            try:
                proc.stdin.write(json.dumps({"say": text, "voice": voice_id}).encode() + b"\n")
                proc.stdin.flush()
                message = json.loads(proc.stdout.readline() or b'{"ok": false, "error": "the voice stopped"}')
            except (OSError, ValueError) as e:
                raise RuntimeError(f"the voice stopped ({e})") from None
            if not message.get("ok"):
                raise RuntimeError(message.get("error") or "the voice failed")
            return int(message["rate"]), proc.stdout.read(int(message["bytes"]))

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc:
            _end(proc)


class TextToSpeech:
    """The voices on this computer and their workers. Methods that start an engine are slow;
    call them from a background thread."""

    def __init__(self, resample: Callable[[bytes, int], bytes] | None = None):
        self._workers = {engine: Worker(engine) for engine in ENGINES}
        self._voices: dict[str, list[Voice]] = {}
        self.errors: dict[str, str] = {}
        self.start_errors: dict[str, str] = {}   # installed engines that failed to start
        self._resample = resample
        self._lock = threading.Lock()
        self.checked = False       # True once refresh() has looked at every engine
        for engine in ENGINES:
            voices = self._cached(engine)
            if voices:
                self._voices[engine] = voices

    @staticmethod
    def _cache_files(engine: str) -> list[Path]:
        return [state_dir() / f"tts-voices-{engine}.json", models_dir() / f"voices-{engine}.json"]

    def _cached(self, engine: str) -> list[Voice]:
        for path in self._cache_files(engine):
            try:
                voices = _voices_from(json.loads(path.read_text()), engine)
            except (OSError, ValueError):
                continue
            if voices:
                return voices
        return []

    def refresh(self) -> None:
        """Find out which engines work, for those without a saved voice list (installer)."""
        for engine in ENGINES:
            if engine in self._voices:
                continue
            voices, error = self._workers[engine].list_voices()
            with self._lock:
                if voices:
                    self._voices[engine] = voices
                    try:
                        path = self._cache_files(engine)[0]
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(json.dumps({"ready": True, "voices": [
                            {"id": v.id, "name": v.name} for v in voices]}))
                    except OSError:
                        pass
                else:
                    self.errors[engine] = error
                    log.info("Text to speech: %s is not available: %s", ENGINE_LABELS[engine], error)
        self.checked = True

    def voices(self) -> list[Voice]:
        with self._lock:
            return [v for engine in ENGINES for v in self._voices.get(engine, [])]

    def pick(self, key: str) -> Voice | None:
        """The voice saved as `key`, or the default: the first natural (Coqui) voice, otherwise
        an English eSpeak voice."""
        voices = self.voices()
        chosen = next((v for v in voices if v.key == key), None)
        if chosen:
            return chosen
        coqui = [v for v in voices if v.engine == "coqui"]
        if coqui:
            return coqui[0]
        english = [v for v in voices if "english" in v.name.lower()]
        return next((v for v in english if "america" in v.name.lower()), english[0] if english else
                    (voices[0] if voices else None))

    def prepare(self, voice: Voice) -> Voice | None:
        """Start `voice`'s engine. If Coqui won't start, falls back to an eSpeak voice (returned);
        None if no voice can be used."""
        if self._workers[voice.engine].start():
            return voice
        worker = self._workers[voice.engine]
        log.warning("Text to speech: %s didn't start: %s", ENGINE_LABELS[voice.engine], worker.error)
        with self._lock:
            self._voices.pop(voice.engine, None)
            self.errors[voice.engine] = self.start_errors[voice.engine] = worker.error
        fallback = self.pick("")
        if fallback and fallback.engine != voice.engine and self._workers[fallback.engine].start():
            return fallback
        return None

    def say(self, voice: Voice, text: str) -> bytes:
        """`text` spoken by `voice` (already prepared), as LinuxComm's 16 kHz PCM."""
        rate, pcm = self._workers[voice.engine].say(text, voice.id)
        return self._resample(pcm, rate) if self._resample else pcm

    def stop(self) -> None:
        """Stop the workers (frees Coqui's memory)."""
        for worker in self._workers.values():
            worker.stop()


class VoiceMessage:
    """Recording a message to be spoken by a synthetic voice.

    While Talk is on, microphone() passes the microphone's audio only to speech to text, and
    on_heard(text) reports what has been recognized so far. finish() stops listening, waits
    for the last words and returns everything recognized; the caller then has it spoken once
    and sends that as one message. start_transcriber(on_result) must return a thread like
    speech.CallTranscriber that calls on_result(call_id, caller, text, final).
    """

    def __init__(self, start_transcriber: Callable, on_heard: Callable[[str], None] | None = None):
        self._sentences: list[str] = []
        self._partial = ""
        self._lock = threading.Lock()
        self._recording = True
        self._on_heard = on_heard
        self.transcriber = start_transcriber(self._on_result)
        if self.transcriber is None:
            raise RuntimeError("speech to text isn't available")

    def microphone(self, pcm: bytes) -> None:
        if self._recording:
            self.transcriber.feed(pcm)

    def _on_result(self, _call_id, _caller, text: str, final: bool) -> None:
        with self._lock:
            if final:
                if text.strip():
                    self._sentences.append(text.strip())
                self._partial = ""
            else:
                self._partial = text.strip()
        if self._on_heard:
            self._on_heard(self.heard())

    def heard(self) -> str:
        """What has been recognized so far, including words still being recognized."""
        with self._lock:
            return " ".join(self._sentences + ([self._partial] if self._partial else []))

    def finish(self, timeout: float = 90.0) -> str:
        """Stop listening, wait for the last words to be recognized, and return the whole text."""
        self._recording = False
        self.transcriber.finish()
        self.transcriber.join(timeout)
        with self._lock:
            return " ".join(self._sentences)


def stream(pcm: bytes, send: Callable[[bytes], None], should_stop: Callable[[], bool] = lambda: False,
           chunk: int = 640) -> None:
    """Send 16 kHz PCM 20 ms at a time in real time, like a live microphone, so the receiving
    station plays it smoothly instead of dropping a burst."""
    next_at = time.monotonic()
    for start in range(0, len(pcm), chunk):
        if should_stop():
            return
        piece = pcm[start:start + chunk]
        send(piece + b"\0" * (chunk - len(piece)))
        next_at += chunk / 32000
        delay = next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        elif delay < -0.2:  # fell behind (a busy computer): don't send a burst to catch up
            next_at = time.monotonic()