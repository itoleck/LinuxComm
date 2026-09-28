"""Offline speech-to-text captions for incoming intercom audio.

Uses Vosk (https://alphacephei.com/vosk) with a small downloadable model. Everything
runs on this computer, and transcripts are only kept in memory. The installer sets
Vosk up; without it, captions are simply unavailable.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import data_dir
from .intercom import SAMPLE_RATE

log = logging.getLogger(__name__)

try:
    import vosk
    vosk.SetLogLevel(-1)  # Kaldi is chatty on stderr
except Exception:  # not installed, or libvosk failed to load
    vosk = None

SESSION_GAP_S = 10 * 60        # a new session starts after this long without incoming speech
QUEUE_SECONDS = 30             # audio buffered per call if recognition falls behind
MODEL_WAIT_S = 60              # how long a call waits for the model to finish loading


def installed() -> bool:
    return vosk is not None


def model_dirs() -> list[Path]:
    """Where speech models are looked for, most specific first."""
    dirs = []
    if os.environ.get("LINUXCOMM_MODELS"):
        dirs.append(Path(os.environ["LINUXCOMM_MODELS"]))
    data = os.environ.get("XDG_DATA_HOME") or os.path.join(Path.home(), ".local", "share")
    dirs += [Path(data) / "linuxcomm" / "models", Path("/opt/linuxcomm/models")]
    return dirs


def find_models() -> list[Path]:
    """Every Vosk model folder (containing am/ and conf/) in model_dirs()."""
    found: list[Path] = []
    for directory in model_dirs():
        try:
            candidates = sorted(directory.iterdir())
        except OSError:
            continue
        for path in candidates:
            if (path / "am").is_dir() and (path / "conf").is_dir() and path not in found:
                found.append(path)
    return found


def model_label(path: str | Path) -> str:
    """"vosk-model-small-en-us-0.15" -> "small-en-us-0.15"."""
    return Path(path).name.removeprefix("vosk-model-")


def is_english(path: str | Path) -> bool:
    return bool(re.search(r"(^|-)en(-|$)", model_label(path)))


def tidy(text: str, english: bool = True) -> str:
    """Vosk's small models give lowercase words without punctuation; make a sentence of them."""
    text = " ".join(text.split())
    if not text:
        return ""
    if english:
        text = re.sub(r"\bi\b", "I", text)  # "i", "i'm", "i'll", ...
    return text[0].upper() + text[1:] + ("" if text[-1] in ".?!" else ".")


# -- transcript -------------------------------------------------------------------------

@dataclass
class Line:
    time: float
    caller: str
    text: str


class Transcript:
    """The captions of the current intercom session, kept in memory only."""

    def __init__(self, session_gap: float = SESSION_GAP_S):
        self.session_gap = session_gap
        self.lines: list[Line] = []
        self.partials: dict[str, Line] = {}   # call id -> words recognized so far
        self.started: float | None = None
        self._last_activity = 0.0

    def _activity(self, now: float) -> bool:
        """Note speech at `now`; return True if that starts a new session."""
        new = self.started is None or now - self._last_activity > self.session_gap
        if new:
            self.lines.clear()
            self.partials.clear()
            self.started = now
        self._last_activity = now
        return new

    def partial(self, call_id: str, caller: str, text: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        new = self._activity(now)
        line = self.partials.get(call_id)
        self.partials[call_id] = Line(line.time if line else now, caller, text)
        return new

    def final(self, call_id: str, caller: str, text: str, now: float | None = None) -> bool:
        """A finished sentence (or "" when a call ends without more speech)."""
        now = time.time() if now is None else now
        started = self.partials.pop(call_id, None)
        if not text:
            return False
        new = self._activity(now)
        self.lines.append(Line(started.time if started else now, caller, text))
        return new

    def clear(self) -> None:
        self.lines.clear()
        self.partials.clear()
        self.started = None

    def live(self) -> list[Line]:
        """Sentences still being spoken, one per call in progress."""
        return [line for line in self.partials.values() if line.text]

    def recent(self, count: int = 2) -> list[tuple[Line, bool]]:
        """The newest `count` sentences, oldest first, as (line, still_being_spoken)."""
        items = [(line, False) for line in self.lines] + [(line, True) for line in self.live()]
        return items[-count:]

    def callers(self) -> list[str]:
        """Who has spoken in this session, in order of first appearance."""
        return list(dict.fromkeys(line.caller for line in self.lines))

    def text(self, format_time: Callable[[float], str]) -> str:
        return "\n".join(format_line(line, format_time) for line in self.lines)


def default_transcript_folder() -> Path:
    """LinuxComm's working folder for saved transcripts: ~/linuxcomm/data."""
    return data_dir()


def format_line(line: Line, format_time: Callable[[float], str]) -> str:
    return f"[{format_time(line.time)}] {line.caller}: {line.text}"


def transcript_filename(started: float, callers: list[str]) -> str:
    """"2026-09-28_14-05-33_Kitchen_Office.txt": when the conversation started, and who spoke."""
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(started))
    names = [re.sub(r"[^\w.-]+", "-", caller).strip("-.") for caller in dict.fromkeys(callers)]
    return f"{stamp}_{'_'.join(n for n in names if n)[:100] or 'unknown'}.txt"


def save_transcript(transcript: Transcript, folder: str | Path, station: str) -> Path:
    """Write the finished sentences to a text file in `folder` and return its path.

    The name comes from the start of the conversation, so saving again later in the
    same conversation updates the file (unless someone new has spoken since).
    """
    if not transcript.lines or transcript.started is None:
        raise ValueError("The transcript is empty")
    folder = Path(folder).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / transcript_filename(transcript.started, transcript.callers())
    header = [
        "LinuxComm transcript",
        f"Received by: {station}",
        f"From: {', '.join(transcript.callers())}",
        f"Started: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(transcript.started))}",
        "",
    ]
    body = transcript.text(lambda ts: time.strftime("%H:%M:%S", time.localtime(ts)))
    path.write_text("\n".join(header) + "\n" + body + "\n", encoding="utf-8")
    return path


# -- recognition --------------------------------------------------------------------------

class VoskBackend:
    def load_model(self, path: Path):
        return vosk.Model(str(path))

    def recognizer(self, model):
        return vosk.KaldiRecognizer(model, SAMPLE_RATE)


class SpeechToText:
    """Loads a model once and transcribes each incoming call on its own thread.

    on_result(call_id, caller, text, final) is called from those threads: partial
    results while someone speaks, a final result for each sentence, and a final
    result with text "" when the call ends.
    """

    def __init__(self, on_result: Callable[[str, str, str, bool], None], backend=None):
        self._on_result = on_result
        self._backend = backend or (VoskBackend() if vosk is not None else None)
        self._lock = threading.Lock()
        self._model = None
        self._model_path: Path | None = None
        self._ready = threading.Event()
        self.error = ""

    @property
    def available(self) -> bool:
        return self._backend is not None

    @property
    def model_path(self) -> Path | None:
        return self._model_path

    @property
    def loading(self) -> bool:
        return self._model_path is not None and not self._ready.is_set()

    @property
    def ready(self) -> bool:
        return self._ready.is_set() and self._model is not None

    def load(self, path: str | Path, on_done: Callable[[], None] | None = None) -> None:
        """Load a model in the background (does nothing if it is already loaded)."""
        path = Path(path)
        if not self.available or path == self._model_path:
            return
        with self._lock:
            self._model, self._model_path, self.error = None, path, ""
            self._ready.clear()

        def run():
            started = time.monotonic()
            try:
                model = self._backend.load_model(path)
                log.info("Loaded speech model %s in %.1f s", path.name, time.monotonic() - started)
            except Exception as e:
                log.error("Could not load speech model %s: %s", path, e)
                model, self.error = None, str(e) or e.__class__.__name__
            with self._lock:
                if path == self._model_path:  # not replaced meanwhile
                    self._model = model
                    self._ready.set()
            if on_done:
                on_done()

        threading.Thread(target=run, name="stt-load", daemon=True).start()

    def unload(self) -> None:
        with self._lock:
            self._model, self._model_path = None, None
            self._ready.clear()

    def start_call(self, call_id: str, caller: str, english: bool = True) -> CallTranscriber | None:
        if not self.available or self._model_path is None:
            return None
        return CallTranscriber(self, call_id, caller, english)

    def _wait_for_model(self):
        if not self._ready.wait(MODEL_WAIT_S):
            return None
        with self._lock:
            return self._model


class CallTranscriber(threading.Thread):
    """Transcribes one incoming call. feed() never blocks the audio."""

    def __init__(self, engine: SpeechToText, call_id: str, caller: str, english: bool):
        super().__init__(name=f"stt:{caller}", daemon=True)
        self._engine = engine
        self.call_id = call_id
        self.caller = caller
        self._english = english
        max_chunks = QUEUE_SECONDS * 100  # 10 ms chunks
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=max_chunks)
        self._dropped = False
        self.start()

    def feed(self, pcm: bytes) -> None:
        try:
            self._queue.put_nowait(pcm)
        except queue.Full:
            if not self._dropped:
                self._dropped = True
                log.warning("Speech recognition can't keep up; skipping audio from %s", self.caller)

    def finish(self) -> None:
        self._queue.put(None)  # may wait briefly if the queue is full

    def _emit(self, text: str, final: bool) -> None:
        try:
            self._engine._on_result(self.call_id, self.caller, tidy(text, self._english) if final else text, final)
        except Exception:
            log.exception("Caption callback failed")

    def run(self) -> None:
        model = self._engine._wait_for_model()
        recognizer = self._engine._backend.recognizer(model) if model is not None else None
        partial = ""
        finished = False
        while not finished:
            chunks = [self._queue.get()]
            while len(chunks) < 20:  # up to ~0.2 s per call into the recognizer
                try:
                    chunks.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            if None in chunks:
                finished = True
                chunks = chunks[:chunks.index(None)]
            if recognizer is None or not chunks:
                continue
            try:
                if recognizer.AcceptWaveform(b"".join(chunks)):
                    self._emit(json.loads(recognizer.Result()).get("text", ""), True)
                    partial = ""
                else:
                    text = json.loads(recognizer.PartialResult()).get("partial", "")
                    if text and text != partial:
                        partial = text
                        self._emit(text, False)
            except Exception:
                log.exception("Speech recognition failed")
                recognizer = None
        if recognizer is not None:
            try:
                self._emit(json.loads(recognizer.FinalResult()).get("text", ""), True)
            except Exception:
                log.exception("Speech recognition failed")
        self._emit("", True)  # the call is over
