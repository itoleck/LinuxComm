"""Tests for talking with a synthetic voice: recording a message (with a stand-in for speech recognition),
sending it in real time, picking voices, and a real pyttsx3 worker where it is installed.

Run with:  python3 -m unittest discover -s tests -v
"""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linuxcomm import tts  # noqa: E402

MIC_NOISE = b"\x7f\x11" * 320        # what the microphone picks up (must never be sent)
VOICE = b"\x01\x02" * 1600            # 100 ms of "synthetic voice"


class FakeTranscriber(threading.Thread):
    """Stands in for speech.CallTranscriber: a chunk b"SAY:<text>" is heard as a finished sentence,
    b"PART:<text>" as words still being recognized; anything else as noise."""

    def __init__(self, on_result):
        super().__init__(daemon=True)
        self.on_result = on_result
        self.chunks: queue.Queue = queue.Queue()
        self.start()

    def feed(self, pcm):
        self.chunks.put(pcm)

    def finish(self):
        self.chunks.put(None)

    def run(self):
        while (chunk := self.chunks.get()) is not None:
            time.sleep(0.02)  # recognition takes a moment
            if chunk.startswith(b"SAY:"):
                self.on_result("call", "Living Room", chunk[4:].decode(), True)
            elif chunk.startswith(b"PART:"):
                self.on_result("call", "Living Room", chunk[5:].decode(), False)
            else:
                self.on_result("call", "Living Room", "", True)  # noise: an empty result
        time.sleep(0.1)
        self.on_result("call", "Living Room", "and that is all", True)   # the last words, at the end
        self.on_result("call", "Living Room", "", True)


class VoiceMessageTests(unittest.TestCase):
    def test_records_everything_and_returns_it_once_at_the_end(self):
        heard = []
        message = tts.VoiceMessage(FakeTranscriber, on_heard=heard.append)
        for chunk in (b"SAY:Dinner is ready.", MIC_NOISE, b"PART:please come", b"SAY:Please come down."):
            message.microphone(chunk)
        time.sleep(0.3)
        self.assertEqual(message.heard(), "Dinner is ready. Please come down.")
        self.assertIn("Dinner is ready. please come", heard, "words still being recognized are shown too")
        text = message.finish()
        self.assertEqual(text, "Dinner is ready. Please come down. and that is all",
                         "each sentence once, in order, with the last words recognized after stopping")

    def test_nothing_after_stopping(self):
        message = tts.VoiceMessage(FakeTranscriber)
        message.microphone(b"SAY:Hello.")
        text = message.finish()
        message.microphone(b"SAY:Too late.")
        self.assertEqual(text, "Hello. and that is all")

    def test_partial_words_alone_are_not_a_message(self):
        class Silent(FakeTranscriber):
            def run(self):
                while self.chunks.get() is not None:
                    self.on_result("call", "Living Room", "mumble", False)
                self.on_result("call", "Living Room", "", True)
        message = tts.VoiceMessage(Silent)
        message.microphone(MIC_NOISE)
        self.assertEqual(message.finish(), "")

    def test_no_speech_to_text_no_message(self):
        with self.assertRaises(RuntimeError):
            tts.VoiceMessage(lambda on_result: None)


class Sent:
    """Collects what is sent, with the time of each piece."""

    def __init__(self):
        self.chunks: list[tuple[float, bytes]] = []

    def __call__(self, chunk):
        self.chunks.append((time.monotonic(), chunk))

    def data(self) -> bytes:
        return b"".join(c for _, c in self.chunks)


class StreamTests(unittest.TestCase):
    def test_real_time_in_20_ms_pieces(self):
        sent = Sent()
        started = time.monotonic()
        tts.stream(VOICE * 10, sent)                        # 1 s of voice
        took = time.monotonic() - started
        self.assertGreater(took, 0.9, "not sent in a burst")
        self.assertLess(took, 1.3)
        self.assertEqual(len(sent.chunks), 50)
        self.assertTrue(all(len(c) == 640 for _, c in sent.chunks))
        self.assertEqual(sent.data(), VOICE * 10)

    def test_the_last_piece_is_padded(self):
        sent = Sent()
        tts.stream(VOICE[:200], sent)                       # 6.25 ms: less than one piece
        self.assertEqual(sent.data(), VOICE[:200] + bytes(440))

    def test_stops_when_told(self):
        sent = Sent()
        tts.stream(VOICE * 50, sent, should_stop=lambda: len(sent.chunks) >= 5)
        self.assertEqual(len(sent.chunks), 5)


class PickVoiceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.mkdtemp()
        self.env = {k: os.environ.get(k) for k in ("XDG_STATE_HOME", "LINUXCOMM_MODELS")}
        os.environ["XDG_STATE_HOME"] = os.path.join(folder, "state")
        os.environ["LINUXCOMM_MODELS"] = os.path.join(folder, "models")
        self.models = Path(os.environ["LINUXCOMM_MODELS"])
        self.models.mkdir()
        self.addCleanup(self.restore)

    def restore(self):
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def save(self, engine, voices):
        (self.models / f"voices-{engine}.json").write_text(json.dumps({"ready": True, "voices": voices}))

    def test_natural_voices_come_first(self):
        self.save("pyttsx3", [{"id": "gmw/en", "name": "English (Great Britain)"},
                              {"id": "gmw/en-US", "name": "English (America)"}])
        self.save("coqui", [{"id": "p225", "name": "Natural voice p225"}])
        voices = tts.TextToSpeech()
        self.assertEqual([v.key for v in voices.voices()], ["coqui:p225", "pyttsx3:gmw/en", "pyttsx3:gmw/en-US"])
        self.assertEqual(voices.pick("").key, "coqui:p225")
        self.assertEqual(voices.pick("pyttsx3:gmw/en").key, "pyttsx3:gmw/en")
        self.assertEqual(voices.pick("coqui:gone").key, "coqui:p225", "a missing voice falls back to the default")
        self.assertEqual(voices.pick("").label, "Natural voice p225 · Coqui")

    def test_without_natural_voices_american_english(self):
        self.save("pyttsx3", [{"id": "gmw/af", "name": "Afrikaans"}, {"id": "gmw/en", "name": "English (Great Britain)"},
                              {"id": "gmw/en-US", "name": "English (America)"}])
        self.assertEqual(tts.TextToSpeech().pick("").key, "pyttsx3:gmw/en-US")

    def test_no_voices(self):
        voices = tts.TextToSpeech()
        self.assertEqual(voices.voices(), [])
        self.assertIsNone(voices.pick(""))


def _has_pyttsx3() -> bool:
    return subprocess.run([tts.worker_python(), "-c", "import pyttsx3"], capture_output=True).returncode == 0


@unittest.skipUnless(_has_pyttsx3(), "pyttsx3 is not installed (the installer adds it)")
class Pyttsx3WorkerTests(unittest.TestCase):
    def test_speaks(self):
        worker = tts.Worker("pyttsx3")
        self.addCleanup(worker.stop)
        voices, error = worker.list_voices()
        self.assertTrue(voices, error)
        self.assertTrue(worker.start(), worker.error)
        for text in ("Dinner is ready.", "Please come down."):   # the worker keeps running
            rate, pcm = worker.say(text, next(v.id for v in voices if "english" in v.name.lower()))
            self.assertGreater(rate, 8000)
            self.assertGreater(len(pcm) / 2 / rate, 0.3, "some speech")
        worker.stop()
        with self.assertRaises(RuntimeError):
            worker.say("Anyone there?", voices[0].id)   # a stopped worker says so


if __name__ == "__main__":
    unittest.main()
