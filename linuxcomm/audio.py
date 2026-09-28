"""Audio capture, playback and device discovery with GStreamer (PipeWire / PulseAudio)."""

from __future__ import annotations

import array
import logging
import math
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from .intercom import BYTES_PER_SECOND, CHANNELS, SAMPLE_RATE  # noqa: E402

Gst.init(None)

log = logging.getLogger(__name__)

RAW_CAPS = f"audio/x-raw,format=S16LE,layout=interleaved,rate={SAMPLE_RATE},channels={CHANNELS}"

PLAYOUT_DELAY = 80 * Gst.MSECOND   # jitter buffer: how far behind arrival audio is played
MAX_AHEAD = 600 * Gst.MSECOND      # if playback falls further behind than this, drop audio


@dataclass(frozen=True)
class AudioDevice:
    id: str      # stable identifier stored in the settings; "" = system default
    name: str    # human-readable


DEFAULT_DEVICE = AudioDevice("", "System default")

# Providers are tried in order. The PulseAudio one talks to PipeWire through
# pipewire-pulse on Ubuntu and is the most mature; PipeWire's own is the fallback.
_PROVIDERS = ("pulsedeviceprovider", "pipewiredeviceprovider")
_FALLBACK_ELEMENTS = {
    "source": ("pulsesrc", "pipewiresrc", "autoaudiosrc"),
    "sink": ("pulsesink", "pipewiresink", "autoaudiosink"),
}


def _make(factory: str) -> Gst.Element:
    element = Gst.ElementFactory.make(factory, None)
    if element is None:
        raise RuntimeError(f"GStreamer element “{factory}” is missing (is gstreamer1.0-plugins-good installed?)")
    return element


def _link_all(pipeline: Gst.Pipeline, elements: list[Gst.Element]) -> None:
    for element in elements:
        pipeline.add(element)
    for a, b in zip(elements, elements[1:]):
        if not a.link(b):
            raise RuntimeError(f"Could not link {a.get_name()} to {b.get_name()}")


def _device_id(device: Gst.Device) -> str:
    try:
        name = device.get_property("internal-name")  # GstPulseDevice
        if name:
            return str(name)
    except TypeError:
        pass
    props = device.get_properties()
    if props:
        for key in ("node.name", "object.path", "device.path"):
            value = props.get_string(key)
            if value:
                return value
    return device.get_display_name()


def _is_monitor(device: Gst.Device) -> bool:
    props = device.get_properties()
    return bool(props and props.get_string("device.class") == "monitor")


class DeviceRegistry:
    """Lists input/output devices and creates GStreamer elements for them."""

    def __init__(self):
        self._lock = threading.Lock()
        self._devices: dict[tuple[str, str], Gst.Device] = {}

    @staticmethod
    def _probe() -> list[Gst.Device]:
        for name in _PROVIDERS:
            provider = Gst.DeviceProviderFactory.get_by_name(name)
            if provider is None:
                continue
            devices = provider.get_devices()
            if devices:
                return list(devices)
        monitor = Gst.DeviceMonitor.new()
        monitor.add_filter("Audio/Source", None)
        monitor.add_filter("Audio/Sink", None)
        return list(monitor.get_devices() or [])

    def refresh(self) -> tuple[list[AudioDevice], list[AudioDevice]]:
        """Return (inputs, outputs); each list starts with DEFAULT_DEVICE."""
        inputs, outputs = [DEFAULT_DEVICE], [DEFAULT_DEVICE]
        found: dict[tuple[str, str], Gst.Device] = {}
        for device in self._probe():
            if device.has_classes("Audio/Source"):
                if _is_monitor(device):
                    continue  # "Monitor of ..." loopback sources are not microphones
                kind, target = "source", inputs
            elif device.has_classes("Audio/Sink"):
                kind, target = "sink", outputs
            else:
                continue
            dev_id = _device_id(device)
            if (kind, dev_id) not in found:
                found[(kind, dev_id)] = device
                target.append(AudioDevice(dev_id, device.get_display_name()))
        with self._lock:
            self._devices = found
        return inputs, outputs

    def make_element(self, kind: str, device_id: str) -> Gst.Element:
        """Create a source ("source") or sink ("sink") element for a device id ("" = default)."""
        device = None
        if device_id:
            with self._lock:
                device = self._devices.get((kind, device_id))
            if device is None:
                self.refresh()
                with self._lock:
                    device = self._devices.get((kind, device_id))
        if device is not None:
            element = device.create_element(None)
            if element is not None:
                return element
        if device_id:
            log.warning("Audio device %r is not available; using the system default", device_id)
        for factory in _FALLBACK_ELEMENTS[kind]:
            element = Gst.ElementFactory.make(factory, None)
            if element is not None:
                return element
        raise RuntimeError("No audio output is available" if kind == "sink" else "No microphone is available")


def pcm_level(pcm: bytes) -> float:
    """Loudness of an S16LE chunk mapped to 0..1 on a -60..0 dBFS scale."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if not samples:
        return 0.0
    if sys.byteorder == "big":
        samples.byteswap()
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    if rms < 1:
        return 0.0
    return max(0.0, min(1.0, (20 * math.log10(rms / 32768) + 60) / 60))


class Capture:
    """Records an input device as 16 kHz mono S16LE.

    on_data(bytes) and on_level(0..1) run on a GStreamer streaming thread;
    on_error(message) runs on the GLib main loop.
    """

    def __init__(self, source: Gst.Element,
                 on_data: Callable[[bytes], None] | None = None,
                 on_level: Callable[[float], None] | None = None,
                 on_error: Callable[[str], None] | None = None):
        self._on_data = on_data
        self._on_level = on_level
        self._on_error = on_error
        self._pipeline = Gst.Pipeline.new(None)
        caps = _make("capsfilter")
        caps.set_property("caps", Gst.Caps.from_string(RAW_CAPS))
        sink = _make("appsink")
        sink.set_property("emit-signals", True)
        sink.set_property("sync", False)
        sink.set_property("max-buffers", 100)
        sink.set_property("drop", True)
        sink.connect("new-sample", self._on_new_sample)
        _link_all(self._pipeline, [source, _make("audioconvert"), _make("audioresample"), caps, sink])
        self._bus = self._pipeline.get_bus()
        self._bus.add_signal_watch()
        self._bus_handler = self._bus.connect("message::error", self._on_bus_error)

    def start(self) -> None:
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            msg = self._bus.pop_filtered(Gst.MessageType.ERROR)
            self.stop()
            raise RuntimeError(msg.parse_error()[0].message if msg else "Could not open the microphone")

    def stop(self) -> None:
        self._pipeline.set_state(Gst.State.NULL)
        if self._bus_handler:
            self._bus.disconnect(self._bus_handler)
            self._bus.remove_signal_watch()
            self._bus_handler = 0

    def _on_new_sample(self, appsink) -> Gst.FlowReturn:
        sample = appsink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        buf = sample.get_buffer()
        pcm = buf.extract_dup(0, buf.get_size())
        try:
            if self._on_data:
                self._on_data(pcm)
            if self._on_level:
                self._on_level(pcm_level(pcm))
        except Exception:
            log.exception("Capture callback failed")
        return Gst.FlowReturn.OK

    def _on_bus_error(self, _bus, message) -> None:
        err, debug = message.parse_error()
        log.error("Capture error: %s (%s)", err.message, debug)
        if self._on_error:
            self._on_error(err.message)


class Player:
    """Plays a stream of 16 kHz mono S16LE PCM on an output device.

    Buffers are timestamped against the system clock with a fixed playout delay,
    which acts as a small jitter buffer for audio arriving over the network.
    With live=True, audio that would play more than MAX_AHEAD late is dropped to
    keep latency bounded; use live=False for clips written all at once.
    write() and close() may be called from any single thread.
    """

    def __init__(self, sink: Gst.Element, volume: float = 1.0, live: bool = True):
        self._live = live
        self._pipeline = Gst.Pipeline.new(None)
        self._clock = Gst.SystemClock.obtain()
        self._pipeline.use_clock(self._clock)
        self._src = _make("appsrc")
        self._src.set_property("caps", Gst.Caps.from_string(RAW_CAPS))
        self._src.set_property("format", Gst.Format.TIME)
        self._src.set_property("is-live", True)
        vol = _make("volume")
        vol.set_property("volume", max(0.0, min(float(volume), 2.0)))
        _link_all(self._pipeline, [self._src, _make("queue"), _make("audioconvert"),
                                   _make("audioresample"), vol, sink])
        self._pending = b""
        self._base_pts = 0
        self._samples = -1          # -1 = not started (or recovering from an underrun)
        self._closed = False
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._pipeline.set_state(Gst.State.NULL)
            raise RuntimeError("Could not open the audio output")

    def _running_time(self) -> int:
        return self._clock.get_time() - self._pipeline.get_base_time()

    def _pts(self, samples: int) -> int:
        return self._base_pts + samples * Gst.SECOND // SAMPLE_RATE

    def write(self, pcm: bytes) -> None:
        if self._closed:
            return
        if self._pending:
            pcm, self._pending = self._pending + pcm, b""
        if len(pcm) % 2:
            pcm, self._pending = pcm[:-1], pcm[-1:]
        if not pcm:
            return
        n = len(pcm) // (2 * CHANNELS)
        now = self._running_time()
        discont = False
        if self._samples < 0 or self._pts(self._samples) < now:
            # First audio, or the network stalled and we ran dry: restart the timeline.
            self._base_pts, self._samples, discont = now + PLAYOUT_DELAY, 0, True
        elif self._live and self._pts(self._samples) - now > MAX_AHEAD:
            return  # far behind real time (sender clock faster than ours): drop to catch up
        buf = Gst.Buffer.new_wrapped(pcm)
        buf.pts = self._pts(self._samples)
        buf.duration = self._pts(self._samples + n) - buf.pts
        if discont:
            buf.set_flags(Gst.BufferFlags.DISCONT)
        self._samples += n
        self._src.emit("push-buffer", buf)

    def close(self) -> None:
        """Play out what is queued, then release the device."""
        if self._closed:
            return
        self._closed = True
        ahead = max(0, self._pts(self._samples) - self._running_time()) if self._samples >= 0 else 0
        self._src.emit("end-of-stream")
        self._pipeline.get_bus().timed_pop_filtered(ahead + Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        self._pipeline.set_state(Gst.State.NULL)


def _make_chime() -> bytes:
    """A soft two-tone "ding-dong" announcing incoming audio."""
    out = array.array("h")
    for freq, dur in ((1318.5, 0.18), (1046.5, 0.40)):  # E6, C6
        n = int(SAMPLE_RATE * dur)
        release = SAMPLE_RATE * 0.02
        for i in range(n):
            t = i / SAMPLE_RATE
            env = min(1.0, t / 0.004, (n - i) / release) * math.exp(-t * 6)
            v = math.sin(2 * math.pi * freq * t) + 0.2 * math.sin(4 * math.pi * freq * t)
            out.append(int(9000 * env * v))
    out.extend([0] * int(SAMPLE_RATE * 0.05))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


CHIME = _make_chime()


def _make_alarm_tone() -> bytes:
    """One cycle of the alarm: four short beeps, then a pause (about 1.3 s)."""
    out = array.array("h")
    beep, gap, pause = int(SAMPLE_RATE * 0.11), int(SAMPLE_RATE * 0.07), int(SAMPLE_RATE * 0.55)
    ramp = SAMPLE_RATE * 0.005
    for _ in range(4):
        for i in range(beep):
            env = min(1.0, i / ramp, (beep - i) / ramp)
            t = i / SAMPLE_RATE
            out.append(int(12000 * env * (math.sin(2 * math.pi * 1000 * t) + 0.3 * math.sin(2 * math.pi * 3000 * t))))
        out.extend([0] * gap)
    out.extend([0] * pause)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


ALARM_TONE = _make_alarm_tone()


class AlarmSound:
    """Plays the alarm tone over and over until stop()."""

    def __init__(self, make_sink: Callable[[], Gst.Element], volume: float = 1.0):
        self._make_sink = make_sink
        self._volume = volume
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="alarm-sound", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            player = Player(self._make_sink(), self._volume)
        except Exception:
            log.exception("Could not play the alarm")
            return
        chunk = BYTES_PER_SECOND // 10
        started, sent = time.monotonic(), 0
        while not self._stop.is_set():
            for i in range(0, len(ALARM_TONE), chunk):
                if self._stop.is_set():
                    break
                data = ALARM_TONE[i:i + chunk]
                player.write(data)
                sent += len(data)
                # Stay about 0.2 s ahead of playback, so the tone is seamless but stops quickly.
                ahead = started + sent / BYTES_PER_SECOND - time.monotonic() - 0.2
                if ahead > 0:
                    self._stop.wait(ahead)
        player.close()


def play_pcm_async(sink: Gst.Element, pcm: bytes, volume: float = 1.0) -> None:
    """Play a short PCM clip (e.g. the chime) on a background thread."""

    def run():
        try:
            player = Player(sink, volume, live=False)
            chunk = BYTES_PER_SECOND // 10
            for i in range(0, len(pcm), chunk):
                player.write(pcm[i:i + chunk])
            player.close()
        except Exception:
            log.exception("Could not play sound")

    threading.Thread(target=run, name="play-clip", daemon=True).start()
