"""Acoustic feedback (howling) detection, used by audio.FeedbackSuppressor.

Kept free of GStreamer so it can be tested on its own.
"""

from __future__ import annotations

import math


class FeedbackDetector:
    """Spots acoustic feedback (howling) in a spectrum.

    Howling is a single frequency far louder than the frequencies around it that stays put,
    unlike speech, whose loudest partials come with neighbouring harmonics and keep moving.
    feed() takes one spectrum frame (magnitudes in dB, band i centred on (i + 0.5) * band_hz)
    and returns the frequency to suppress once the same peak has lasted `frames` frames.
    """

    def __init__(self, band_hz: float, min_hz: float = 150.0, max_hz: float = 7500.0, frames: int = 6,
                 peak_db: float = 18.0, min_db: float = -45.0, span: int = 12):
        self.band_hz = band_hz
        self.min_hz, self.max_hz = min_hz, max_hz
        self.frames = frames
        self.peak_db = peak_db    # how far the peak must stand above its surroundings
        self.min_db = min_db      # quieter peaks are ignored
        self.span = span          # surroundings: this many bands each side (the 2 next to the peak excluded)
        self._bin = -10
        self._streak = 0

    def reset(self) -> None:
        self._bin, self._streak = -10, 0

    def feed(self, magnitudes) -> float | None:
        lo = max(1, int(self.min_hz / self.band_hz))
        hi = min(len(magnitudes) - 1, int(self.max_hz / self.band_hz))
        if hi <= lo:
            return None
        peak = max(range(lo, hi + 1), key=magnitudes.__getitem__)
        level = magnitudes[peak]
        around = [magnitudes[i] for i in range(max(0, peak - self.span), min(len(magnitudes), peak + self.span + 1))
                  if abs(i - peak) > 2]
        mean = 10 * math.log10(max(sum(10 ** (m / 10) for m in around) / len(around), 1e-12))
        if level < self.min_db or level - mean < self.peak_db:
            self._streak = 0
            return None
        if abs(peak - self._bin) <= 1:  # still where this peak started (a gliding tone moves on)
            self._streak += 1
        else:
            self._bin, self._streak = peak, 1
        if self._streak < self.frames:
            return None
        self._streak = 0
        return (peak + 0.5) * self.band_hz
