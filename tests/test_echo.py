"""Tests for echo delay estimation (the part of echo cancellation that needs no GStreamer).

Run with:  python3 -m unittest discover -s tests -v
"""

import array
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linuxcomm.echo import FRAME_NS, EchoDelayEstimator, frame_levels, webrtc_finds_delay  # noqa: E402

RATE = 16000


def speech_like(seconds: float, seed: int = 1) -> list[float]:
    """Syllables of noise at varying loudness and length, with pauses."""
    rng = random.Random(seed)
    out: list[float] = []
    while len(out) < seconds * RATE:
        gain = rng.uniform(1000, 4000)
        out += [rng.gauss(0, gain) for _ in range(int(rng.uniform(0.08, 0.35) * RATE))]
        out += [0.0] * int(rng.uniform(0.04, 0.25) * RATE)
    return out[: int(seconds * RATE)]


def pcm(samples) -> bytes:
    return array.array("h", (max(-32768, min(32767, int(s))) for s in samples)).tobytes()


def feed(estimator, speaker, microphone, start_ns=5 * 10**9):
    """Both signals in 100 ms chunks, timestamped on a shared clock."""
    step = RATE // 10
    for i in range(0, len(speaker), step):
        pts = start_ns + i * FRAME_NS // 160
        estimator.add("speaker", pts, pcm(speaker[i:i + step]))
        estimator.add("microphone", pts, pcm(microphone[i:i + step]))


class WebRtcVersionTests(unittest.TestCase):
    """Which WebRTC library webrtcdsp loaded decides whether LinuxComm measures the echo delay."""

    def maps(self, *libraries):
        return "".join(f"7f00f9a41000-7f00f9b41000 r-xp 00000000 08:20 1234 /usr/lib/aarch64-linux-gnu/{lib}\n"
                       for lib in ("libc.so.6", "libgstwebrtcdsp.so", *libraries))

    def test_new_library_finds_the_delay(self):  # Debian 13 / Raspberry Pi OS Trixie, Ubuntu 26.04, Arch
        self.assertTrue(webrtc_finds_delay(self.maps("libwebrtc-audio-processing-1.so.3")))
        self.assertTrue(webrtc_finds_delay(self.maps("libwebrtc-audio-processing-2.so.1")))

    def test_old_library_needs_to_be_told(self):  # Debian 12 / Raspberry Pi OS Bookworm
        self.assertFalse(webrtc_finds_delay(self.maps("libwebrtc_audio_processing.so.1.0.0")))

    def test_unknown_means_new(self):
        self.assertTrue(webrtc_finds_delay(""))


class EchoDelayTests(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(len(frame_levels(pcm([0] * 1600))), 10)       # 100 ms = 10 frames
        loud, quiet = frame_levels(pcm([8000, -8000] * 80 + [10, -10] * 80))
        self.assertGreater(loud - quiet, 50)

    def test_finds_the_delay(self):
        speaker = speech_like(6)
        noise = random.Random(7)
        for delay_ms in (0, 40, 130, 260, 450):
            d = delay_ms * RATE // 1000
            mic = [0.4 * (speaker[i - d] if i >= d else 0) + noise.gauss(0, 30) for i in range(len(speaker))]
            estimator = EchoDelayEstimator()
            feed(estimator, speaker, mic)
            found = estimator.estimate()
            self.assertIsNotNone(found, delay_ms)
            self.assertAlmostEqual(found[0], delay_ms, delta=10, msg=delay_ms)
            self.assertGreater(found[1], 0.8)

    def test_no_echo_no_answer(self):
        speaker = speech_like(6, seed=2)
        other_voice = speech_like(6, seed=3)                            # the microphone hears someone else
        estimator = EchoDelayEstimator()
        feed(estimator, speaker, other_voice)
        self.assertIsNone(estimator.estimate())

    def test_a_silent_speaker_teaches_nothing(self):
        estimator = EchoDelayEstimator()
        feed(estimator, [0.0] * RATE * 6, speech_like(6))
        self.assertIsNone(estimator.estimate())
        self.assertIsNone(EchoDelayEstimator().estimate(), "no audio yet")

    def test_old_audio_is_forgotten(self):
        estimator = EchoDelayEstimator(window_ms=1000, max_delay_ms=200)
        feed(estimator, speech_like(20), speech_like(20, seed=5))
        for levels in estimator._levels.values():
            self.assertLessEqual(len(levels), 2 * (100 + 20) + 10)


if __name__ == "__main__":
    unittest.main()
