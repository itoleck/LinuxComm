"""Tests for muting other applications during incoming audio, against a fake sound server.

Run with:  python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linuxcomm import muting  # noqa: E402

OWN_PID = str(os.getpid())


class FakeServer:
    """Pretends to be PipeWire/PulseAudio: a set of playback streams."""

    def __init__(self, streams):
        self.lock = threading.Lock()
        self.streams = {s["index"]: dict(s) for s in streams}

    def add(self, index, pid, app, muted=False):
        with self.lock:
            self.streams[index] = {"index": index, "pid": pid, "app": app, "muted": muted}

    def remove(self, index):
        with self.lock:
            del self.streams[index]

    def muted(self):
        with self.lock:
            return {i for i, s in self.streams.items() if s["muted"]}

    # backend API
    def streams_list(self):
        with self.lock:
            return [dict(s) for s in self.streams.values()]

    def set_mute(self, index, mute):
        with self.lock:
            if index not in self.streams:
                raise OSError("no such stream")
            self.streams[index]["muted"] = mute


class Backend:
    def __init__(self, server):
        self.server = server

    def streams(self):
        return self.server.streams_list()

    def set_mute(self, index, mute):
        self.server.set_mute(index, mute)


def wait_for(condition, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class MuterTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer([
            {"index": 1, "pid": "100", "app": "Music", "muted": False},
            {"index": 2, "pid": "200", "app": "Video", "muted": True},     # muted by the user
            {"index": 3, "pid": OWN_PID, "app": "LinuxComm", "muted": False},  # our own playback
        ])
        self.state = Path(tempfile.mkdtemp()) / "muted.json"
        self.enabled = True
        self.muter = self.make_muter()
        self.muter.start()

    def make_muter(self):
        return muting.OtherAppsMuter(lambda: self.enabled, state_file=self.state, backend=Backend(self.server),
                                     release_delay=0.3, rescan_interval=0.05)

    def tearDown(self):
        self.muter.stop()

    def test_mutes_others_and_restores_only_what_it_muted(self):
        self.muter.acquire()
        self.assertTrue(wait_for(lambda: self.server.muted() == {1, 2}))  # not our own stream (3)
        self.muter.release()
        time.sleep(0.15)
        self.assertEqual(self.server.muted(), {1, 2}, "should wait for the release delay")
        self.assertTrue(wait_for(lambda: self.server.muted() == {2}))  # the user's own mute stays

    def test_streams_started_during_a_call_are_muted_too(self):
        self.muter.acquire()
        self.assertTrue(wait_for(lambda: 1 in self.server.muted()))
        self.server.add(4, "400", "Browser")
        self.assertTrue(wait_for(lambda: 4 in self.server.muted()))
        self.muter.release()
        self.assertTrue(wait_for(lambda: self.server.muted() == {2}))

    def test_back_and_forth_does_not_unmute_between_calls(self):
        self.muter.acquire()
        self.assertTrue(wait_for(lambda: 1 in self.server.muted()))
        self.muter.release()
        time.sleep(0.1)          # a reply starts before the release delay ends
        self.muter.acquire()
        time.sleep(0.4)
        self.assertIn(1, self.server.muted())
        self.muter.release()
        self.assertTrue(wait_for(lambda: 1 not in self.server.muted()))

    def test_overlapping_calls(self):
        self.muter.acquire()
        self.muter.acquire()
        self.assertTrue(wait_for(lambda: 1 in self.server.muted()))
        self.muter.release()
        time.sleep(0.5)
        self.assertIn(1, self.server.muted(), "one call is still active")
        self.muter.release()
        self.assertTrue(wait_for(lambda: 1 not in self.server.muted()))

    def test_stream_that_ended_or_changed_owner_is_left_alone(self):
        self.muter.acquire()
        self.assertTrue(wait_for(lambda: 1 in self.server.muted()))
        self.server.remove(1)
        self.server.add(1, "999", "Other app", muted=True)  # index reused by another app
        self.muter.release()
        time.sleep(0.6)
        self.assertIn(1, self.server.muted())

    def test_disabled_does_nothing(self):
        self.enabled = False
        self.muter.acquire()
        time.sleep(0.3)
        self.assertEqual(self.server.muted(), {2})
        self.muter.release()

    def test_stop_unmutes_immediately(self):
        self.muter.acquire()
        self.assertTrue(wait_for(lambda: 1 in self.server.muted()))
        self.muter.stop()
        self.assertEqual(self.server.muted(), {2})
        self.assertFalse(self.state.exists())

    def test_recovers_after_a_crash(self):
        self.muter.acquire()
        self.assertTrue(wait_for(lambda: 1 in self.server.muted() and self.state.exists()))
        # Simulate a crash: the worker dies without restoring, and a new instance starts.
        crashed = self.muter
        crashed._restore = lambda: None
        crashed.stop()
        self.assertIn(1, self.server.muted())
        self.muter = self.make_muter()
        self.muter.start()
        self.assertTrue(wait_for(lambda: self.server.muted() == {2}))
        self.assertTrue(wait_for(lambda: not self.state.exists()))

    def test_missing_pactl_is_harmless(self):
        class Missing:
            def streams(self):
                raise FileNotFoundError("pactl")

            def set_mute(self, index, mute):
                raise FileNotFoundError("pactl")

        muter = muting.OtherAppsMuter(lambda: True, backend=Missing(), release_delay=0.05, rescan_interval=0.05)
        muter.start()
        with self.assertLogs("linuxcomm.muting", "WARNING") as logs:
            muter.acquire()
            time.sleep(0.2)
            muter.release()
            time.sleep(0.2)
            muter.stop()
        self.assertEqual(len(logs.records), 1, "warn once, not on every rescan")


if __name__ == "__main__":
    unittest.main()
