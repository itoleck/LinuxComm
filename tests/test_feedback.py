"""Tests for acoustic feedback detection (howling) on synthetic spectra.

Run with:  python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linuxcomm.feedback import FeedbackDetector  # noqa: E402

BANDS, BAND_HZ = 256, 8000 / 256   # as audio.FeedbackSuppressor uses it: 31.25 Hz per band
FLOOR = -70.0


def spectrum(peaks: dict[float, float]) -> list[float]:
    """A spectrum frame with the given {frequency: dB} peaks over a quiet floor."""
    frame = [FLOOR] * BANDS
    for freq, level in peaks.items():
        frame[int(freq / BAND_HZ)] = level
    return frame


def voice(f0: float, formant: float = 600.0, level: float = -20.0) -> list[float]:
    """A vowel: harmonics of f0, loudest near the formant, falling off 6 dB per harmonic away from it."""
    peaks, n = {}, 1
    while n * f0 < 7500:
        peaks[n * f0] = level - 6 * abs(round((n * f0 - formant) / f0))
        n += 1
    return spectrum(peaks)


def feed(detector, frames):
    return [f for f in (detector.feed(frame) for frame in frames) if f is not None]


class FeedbackDetectorTests(unittest.TestCase):
    def test_a_steady_howl_is_found(self):
        detector = FeedbackDetector(BAND_HZ)
        found = feed(detector, [spectrum({2500: -15})] * 6)
        self.assertEqual(len(found), 1, "after 6 frames (0.3 s)")
        self.assertAlmostEqual(found[0], 2500, delta=BAND_HZ)
        self.assertEqual(feed(detector, [spectrum({2500: -15})] * 5), [], "the next report needs 6 more frames")
        self.assertEqual(len(feed(detector, [spectrum({2500: -15})])), 1)

    def test_a_howl_drifting_by_a_band_still_counts(self):
        frames = [spectrum({1000 + (i % 2) * BAND_HZ: -15}) for i in range(6)]
        self.assertEqual(len(feed(FeedbackDetector(BAND_HZ), frames)), 1)

    def test_speech_is_left_alone(self):
        detector = FeedbackDetector(BAND_HZ)
        for f0 in (95, 120, 180, 220, 260):            # steady vowels, low to high voices
            self.assertEqual(feed(detector, [voice(f0)] * 20), [], f0)
        intonation = [voice(110 + 5 * i) for i in range(20)]         # a voice rising in pitch
        self.assertEqual(feed(detector, intonation), [])
        whistle = [spectrum({900 + 40 * i: -15}) for i in range(20)]  # a pure tone that keeps moving
        self.assertEqual(feed(detector, whistle), [])

    def test_quiet_or_out_of_range_tones_are_ignored(self):
        detector = FeedbackDetector(BAND_HZ)
        self.assertEqual(feed(detector, [spectrum({1500: -60})] * 20), [], "too quiet")
        self.assertEqual(feed(detector, [spectrum({100: -10})] * 20), [], "below 150 Hz (hum)")
        self.assertEqual(feed(detector, [[-90.0] * BANDS] * 20), [], "silence")

    def test_an_interruption_restarts_the_count(self):
        detector = FeedbackDetector(BAND_HZ)
        frames = [spectrum({3000: -15})] * 5 + [voice(150)] + [spectrum({3000: -15})] * 5
        self.assertEqual(feed(detector, frames), [])
        detector.reset()
        self.assertEqual(feed(detector, [spectrum({3000: -15})] * 5), [])


class SpectrumMessageTests(unittest.TestCase):
    def test_magnitudes_are_read_from_the_message(self):
        try:
            from linuxcomm import audio
            from gi.repository import Gst
        except (ImportError, ValueError) as e:
            self.skipTest(f"GStreamer is not available: {e}")
        structure = Gst.Structure.new_from_string("spectrum, endtime=(guint64)50000000, "
                                                  "magnitude=(float){ -90, -12.5, -3 };")
        self.assertEqual(audio.spectrum_magnitudes(structure), [-90.0, -12.5, -3.0])


if __name__ == "__main__":
    unittest.main()
