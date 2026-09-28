"""Tests for captions: sentence tidying, sessions, and the recognition threads (fake engine).

Run with:  python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linuxcomm import speech  # noqa: E402


class TidyTests(unittest.TestCase):
    def test_sentences(self):
        self.assertEqual(speech.tidy("dinner is ready"), "Dinner is ready.")
        self.assertEqual(speech.tidy("  i think i'm   late "), "I think I'm late.")
        self.assertEqual(speech.tidy("is it ready?"), "Is it ready?")
        self.assertEqual(speech.tidy(""), "")
        self.assertEqual(speech.tidy("i giorni", english=False), "I giorni.")
        self.assertEqual(speech.tidy("ho visto i gatti", english=False), "Ho visto i gatti.")

    def test_model_names(self):
        self.assertEqual(speech.model_label("/x/vosk-model-small-en-us-0.15"), "small-en-us-0.15")
        self.assertTrue(speech.is_english("vosk-model-small-en-us-0.15"))
        self.assertTrue(speech.is_english("vosk-model-en-in-0.5"))
        self.assertFalse(speech.is_english("vosk-model-small-de-0.15"))


class TranscriptTests(unittest.TestCase):
    def test_partial_then_final(self):
        t = speech.Transcript()
        self.assertTrue(t.partial("c1", "Kitchen", "dinner is", now=100))  # starts a session
        self.assertEqual([(l.text, p) for l, p in t.recent()], [("dinner is", True)])
        t.final("c1", "Kitchen", "Dinner is ready.", now=102)
        self.assertEqual([(l.text, p) for l, p in t.recent()], [("Dinner is ready.", False)])
        self.assertEqual(t.lines[0].time, 100, "a sentence is timed from when it started")

    def test_recent_shows_the_last_two(self):
        t = speech.Transcript()
        for i in range(4):
            t.final("c1", "Kitchen", f"Sentence {i}.", now=100 + i)
        t.partial("c1", "Kitchen", "and now", now=105)
        self.assertEqual([l.text for l, _ in t.recent()], ["Sentence 3.", "and now"])

    def test_two_callers_at_once(self):
        t = speech.Transcript()
        t.partial("a", "Kitchen", "hello", now=100)
        t.partial("b", "Office", "hi there", now=100.5)
        self.assertEqual([(l.caller, l.text) for l, _ in t.recent()], [("Kitchen", "hello"), ("Office", "hi there")])
        t.final("a", "Kitchen", "", now=101)  # Kitchen's call ended without another sentence
        self.assertEqual([l.caller for l, _ in t.recent()], ["Office"])

    def test_sessions(self):
        t = speech.Transcript(session_gap=600)
        t.final("c1", "Kitchen", "First.", now=1000)
        self.assertFalse(t.final("c2", "Office", "Second.", now=1500))
        self.assertTrue(t.final("c3", "Kitchen", "Much later.", now=2200))  # > 10 min of quiet
        self.assertEqual([l.text for l in t.lines], ["Much later."])
        self.assertEqual(t.started, 2200)

    def test_text(self):
        t = speech.Transcript()
        t.final("c1", "Kitchen", "Dinner is ready.", now=100)
        t.final("c2", "Office", "Coming.", now=110)
        self.assertEqual(t.text(lambda ts: str(int(ts))), "[100] Kitchen: Dinner is ready.\n[110] Office: Coming.")

    def test_live_and_callers(self):
        t = speech.Transcript()
        t.final("c1", "Kitchen", "One.", now=100)
        t.final("c2", "Office", "Two.", now=101)
        t.final("c3", "Kitchen", "Three.", now=102)
        t.partial("c4", "Garage", "still talk", now=103)
        t.partial("c5", "Porch", "", now=103)
        self.assertEqual(t.callers(), ["Kitchen", "Office"])
        self.assertEqual([(l.caller, l.text) for l in t.live()], [("Garage", "still talk")])


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp()) / "data"  # created on save
        self.started = time.mktime((2026, 9, 28, 14, 5, 33, 0, 0, -1))

    def test_filename(self):
        name = speech.transcript_filename(self.started, ["Kitchen", "Living Room", "Kitchen", "Küche/2"])
        self.assertEqual(name, "2026-09-28_14-05-33_Kitchen_Living-Room_Küche-2.txt")
        self.assertEqual(speech.transcript_filename(self.started, ["../.."]), "2026-09-28_14-05-33_unknown.txt")

    def test_save(self):
        t = speech.Transcript()
        t.final("c1", "Kitchen", "Dinner is ready.", now=self.started)
        t.final("c2", "Office", "Coming.", now=self.started + 5)
        t.partial("c3", "Kitchen", "and bring", now=self.started + 9)  # not finished: not saved
        path = speech.save_transcript(t, self.folder, "Living Room")
        self.assertEqual(path, self.folder / "2026-09-28_14-05-33_Kitchen_Office.txt")
        self.assertEqual(path.read_text(encoding="utf-8"), (
            "LinuxComm transcript\n"
            "Received by: Living Room\n"
            "From: Kitchen, Office\n"
            "Started: 2026-09-28 14:05:33\n"
            "\n"
            "[14:05:33] Kitchen: Dinner is ready.\n"
            "[14:05:38] Office: Coming.\n"))
        # Saving again later in the same conversation updates the same file.
        t.final("c3", "Kitchen", "And bring plates.", now=self.started + 12)
        self.assertEqual(speech.save_transcript(t, self.folder, "Living Room"), path)
        self.assertIn("And bring plates.", path.read_text(encoding="utf-8"))

    def test_default_folder_is_in_the_home_folder(self):
        old = os.environ.get("HOME")
        os.environ["HOME"] = "/home/pi"
        try:
            self.assertEqual(speech.default_transcript_folder(), Path("/home/pi/linuxcomm/data"))
        finally:
            os.environ["HOME"] = old

    def test_nothing_to_save(self):
        with self.assertRaisesRegex(ValueError, "empty"):
            speech.save_transcript(speech.Transcript(), self.folder, "Living Room")

    def test_folder_without_permission(self):
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        parent = Path(tempfile.mkdtemp())
        parent.chmod(0o555)
        self.addCleanup(parent.chmod, 0o755)
        t = speech.Transcript()
        t.final("c1", "Kitchen", "Hello.", now=self.started)
        with self.assertRaises(PermissionError):
            speech.save_transcript(t, parent / "data", "Living Room")


class FakeRecognizer:
    """Treats the audio bytes as text: '.' ends a sentence."""

    def __init__(self):
        self.buffer = b""
        self.done = ""

    def AcceptWaveform(self, data):
        self.buffer += data
        if b"." in self.buffer:
            self.done, _, self.buffer = self.buffer.partition(b".")
            return True
        return False

    def Result(self):
        return json.dumps({"text": self.done.decode().strip()})

    def PartialResult(self):
        return json.dumps({"partial": self.buffer.decode().strip()})

    def FinalResult(self):
        text, self.buffer = self.buffer.decode().strip(), b""
        return json.dumps({"text": text})


class FakeBackend:
    def __init__(self, load_delay=0.0, fail=False):
        self.load_delay, self.fail = load_delay, fail

    def load_model(self, path):
        time.sleep(self.load_delay)
        if self.fail:
            raise RuntimeError("bad model")
        return object()

    def recognizer(self, model):
        return FakeRecognizer()


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.results = []
        self.done = threading.Event()

    def on_result(self, call_id, caller, text, final):
        self.results.append((caller, text, final))
        if final and text == "":
            self.done.set()

    def run_call(self, backend, chunks):
        engine = speech.SpeechToText(self.on_result, backend=backend)
        engine.load("/models/vosk-model-small-en-us-0.15")
        call = engine.start_call("c1", "Kitchen")
        for chunk in chunks:
            call.feed(chunk)
            time.sleep(0.01)
        call.finish()
        self.assertTrue(self.done.wait(5))
        return engine

    def test_partials_sentences_and_end(self):
        self.run_call(FakeBackend(), [b"dinner ", b"is ready. ", b"please ", b"come"])
        finals = [text for _, text, final in self.results if final]
        self.assertEqual(finals[:2], ["Dinner is ready.", "Please come."])
        self.assertEqual(finals[-1], "")
        partials = [text for _, text, final in self.results if not final]
        self.assertIn("dinner", partials[0])

    def test_audio_arriving_before_the_model_is_loaded_is_kept(self):
        self.run_call(FakeBackend(load_delay=0.3), [b"early words. "])
        self.assertIn("Early words.", [text for _, text, final in self.results if final])

    def test_a_model_that_fails_to_load(self):
        engine = self.run_call(FakeBackend(fail=True), [b"lost. "])
        self.assertEqual([t for _, t, f in self.results], [""])  # just the end of the call
        self.assertIn("bad model", engine.error)
        self.assertFalse(engine.ready)

    def test_without_a_model_there_is_no_transcriber(self):
        engine = speech.SpeechToText(self.on_result, backend=FakeBackend())
        self.assertIsNone(engine.start_call("c1", "Kitchen"))

    def test_find_models(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "vosk-model-small-en-us-0.15" / "am").mkdir(parents=True)
            (Path(root) / "vosk-model-small-en-us-0.15" / "conf").mkdir()
            (Path(root) / "not-a-model").mkdir()
            old = os.environ.get("LINUXCOMM_MODELS")
            os.environ["LINUXCOMM_MODELS"] = root
            try:
                self.assertEqual([p.name for p in speech.find_models()][:1], ["vosk-model-small-en-us-0.15"])
            finally:
                if old is None:
                    del os.environ["LINUXCOMM_MODELS"]
                else:
                    os.environ["LINUXCOMM_MODELS"] = old


if __name__ == "__main__":
    unittest.main()
