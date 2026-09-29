"""Echo delay estimation, used by audio.EchoCanceller.

Kept free of GStreamer so it can be tested on its own.
"""

from __future__ import annotations

import array
import math
import sys
import threading

FRAME_NS = 10_000_000          # 10 ms
FRAME_BYTES = 320              # 10 ms of 16 kHz mono S16LE


def frame_levels(pcm: bytes) -> list[float]:
    """The loudness (dB) of each 10 ms frame of 16 kHz mono S16LE audio."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // FRAME_BYTES * FRAME_BYTES])
    if sys.byteorder == "big":
        samples.byteswap()
    step = FRAME_BYTES // 2
    return [10 * math.log10(sum(s * s for s in samples[i:i + step]) / step + 1)
            for i in range(0, len(samples), step)]


def webrtc_finds_delay(maps: str) -> bool:
    """Whether the loaded WebRTC audio processing finds the echo delay by itself, judged from the
    libraries mapped into the process (the text of /proc/self/maps).

    webrtc-audio-processing 1.x and 2.x (AEC3: Debian 13 / Raspberry Pi OS Trixie, Ubuntu 25.10+, Arch)
    do, and must not be told otherwise. The old 0.3 library (Debian 12 / Raspberry Pi OS Bookworm,
    file name with underscores) only looks within a few tens of milliseconds of the delay it's given.
    """
    return "libwebrtc_audio_processing.so" not in maps


def _correlation(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else 0.0


class EchoDelayEstimator:
    """Finds how long after the speaker plays something it comes back in the microphone.

    Both signals are reduced to their loudness every 10 ms (timestamped in nanoseconds on a
    shared clock); the delay is the lag at which the microphone's loudness follows the
    speaker's most closely. add() may be called from any thread.
    """

    def __init__(self, max_delay_ms: int = 500, window_ms: int = 4000, min_correlation: float = 0.6):
        self.max_lag = max_delay_ms // 10
        self.window = window_ms // 10
        self.min_correlation = min_correlation
        self._levels: dict[str, dict[int, float]] = {"speaker": {}, "microphone": {}}
        self._lock = threading.Lock()

    def add(self, which: str, pts_ns: int, pcm: bytes) -> None:
        """Audio from "speaker" or "microphone", starting at pts_ns."""
        start = pts_ns // FRAME_NS
        levels = frame_levels(pcm)
        with self._lock:
            store = self._levels[which]
            for i, level in enumerate(levels):
                store[start + i] = level
            if len(store) > 2 * (self.window + self.max_lag):  # forget the oldest
                keep = start + len(levels) - (self.window + self.max_lag)
                for frame in [f for f in store if f < keep]:
                    del store[frame]

    def estimate(self) -> tuple[int, float] | None:
        """(delay in ms, correlation) over the last few seconds, or None if it isn't clear."""
        with self._lock:
            speaker, mic = dict(self._levels["speaker"]), dict(self._levels["microphone"])
        if not speaker or not mic:
            return None
        end = min(max(mic) - self.max_lag, max(speaker))
        frames = [f for f in range(end - self.window, end + 1) if f in speaker]
        if len(frames) < self.window // 2:
            return None
        levels = [speaker[f] for f in frames]
        mean = sum(levels) / len(levels)
        if math.sqrt(sum((x - mean) ** 2 for x in levels) / len(levels)) < 3:
            return None  # the speaker was (nearly) silent or steady: nothing to learn from
        best: tuple[int, float] | None = None
        for lag in range(self.max_lag + 1):
            pairs = [(speaker[f], mic[f + lag]) for f in frames if f + lag in mic]
            if len(pairs) < len(frames) * 0.8:
                continue
            r = _correlation([p[0] for p in pairs], [p[1] for p in pairs])
            if best is None or r > best[1]:
                best = (lag, r)
        if best is None or best[1] < self.min_correlation:
            return None
        return best[0] * 10, best[1]
